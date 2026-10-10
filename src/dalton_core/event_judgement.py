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

from .final_text_contract import final_text_instructions

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from .cockpit_model import (
    CockpitModelError,
    independent_model_call,
    lane_status_for,
    model_failure_trace,
    register_purpose,
    unwrap_json_object,
)
from .claim_index_authority import EVIDENCE_KINDS, EVIDENCE_KIND_DEFINITIONS
from .research_event import EVIDENCE_TIERS, worst_tier
from .research_playbook import DECISION_VOCABULARY
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
EVENT_PROMPT_CONTRACT_VERSION = "event-output-constraints:0.8"
_SCHEMA_PATH = Path(__file__).with_name("event_judgement_schema.sql")

# Registered at import: anything that can call ``judge()`` has already run this
# line, and a purpose registered after the call that uses it is not registered.
PURPOSE = register_purpose("event_judgement")
VERIFIER_PURPOSE = register_purpose("event_judgement_verifier")
# Named separately from the judgement because it is a different question with a
# different output shape, and because the owner should be able to read what the
# reflection layer costs as its own line in the day ledger rather than as part
# of the judgement's.
REFLECTION_PURPOSE = register_purpose("thesis_reflection")
REFLECTION_VERIFIER_PURPOSE = register_purpose("thesis_reflection_verifier")

#: W3: the three things that can have happened to a prior view. The same three
#: words the Initial Screen drafter is told to sort prior questions into, so a
#: reader moving between the two documents is reading one vocabulary.
PRIOR_VIEW_VERDICTS: tuple[str, ...] = ("still_holds", "changed", "resolved")
_PRIOR_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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
#: Every cockpit purpose that books into ``event_response_spend`` -- this lane's
#: four and the earnings season's four (P14f shares the pool and the book) --
#: and so every purpose whose day-ledger spend :func:`pool_state` sums.  The
#: earnings names are spelled here rather than imported because
#: ``earnings_season`` imports this module.
POOL_LEDGER_PURPOSES: tuple[str, ...] = (
    "event_judgement", "event_judgement_verifier", "event_triage",
    "thesis_reflection", "thesis_reflection_verifier",
    "earnings_preview", "earnings_preview_verifier",
    "earnings_calibration", "earnings_calibration_verifier",
)

MAX_BECAUSE_CHARS = 1200
MAX_NOTE_CHARS = 1200
MAX_NOTE_SENTENCES = 4

# A NO_CHANGE judgement is, by definition, the view staying where it was.  Live
# (2026-09-23) ACN's event_note chain gained nine versions in four hours, every
# one NO_CHANGE and each a paraphrase of the last; across the ledger 99.8% of
# judgements are NO_CHANGE.  So a NO_CHANGE never mints a note of its own: the
# judgement is recorded exactly as before and carries this marker, and once
# the UTC day has closed the company's NO_CHANGE inputs are rolled into at most
# one digest version on the same event_note chain.  Every other decision still
# publishes at once.  The marker is what the digest selects on, which is also
# why a deploy does not backfill a digest for every historical day.
DAILY_DIGEST_MARKER = "event-note-daily-digest:v1"
DAILY_DIGEST_TEMPLATE_REF = "template:event-note-daily-digest:v1"
DAILY_DIGEST_LOOKBACK_DAYS = 7
DAILY_DIGEST_MAX_ENTRIES = 22
#: Batch F: a digest sentence whose figure nothing in this Core sources is left
#: out (never replaced by an indirect placeholder); this line counts them.
DAILY_DIGEST_DROPPED_NOTE = "另有 {n} 句含无法溯源的数字，未收入本摘要（完整理由见本节标题所指的研判记录）。"
DAILY_DIGEST_EMPTY_NOTE = "研判理由的每一句都含无法溯源的数字，正文从略（完整理由见本节标题所指的研判记录）。"
MAX_REFS = 12
MAX_RECENT_JUDGEMENTS = 5
MAX_CLAIMS_IN_PROMPT = 12
MAX_PROMPT_CHARS = 60_000
MAX_PROMPT_BYTES = 60_000


def _bounded_prompt(
    value: str,
    *,
    preserve_from: str = "Return raw JSON only",
    max_bytes: int = MAX_PROMPT_BYTES,
) -> str:
    """Preserve the complete prompt or refuse it whole at the governed byte bound."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    raise EventJudgementValidationError(
        "the complete event prompt exceeds the model input bound "
        f"({len(encoded)}>{max_bytes} bytes)"
    )


def _cited_evidence_lines(context: Mapping[str, Any], citations: Sequence[str]) -> list[str]:
    """Render complete context records named by a producer's citations."""
    wanted = set(citations)
    found: dict[str, Mapping[str, Any]] = {}
    pending: list[Any] = [context]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            ref = value.get("ref") or value.get("id")
            if isinstance(ref, str) and ref in wanted:
                found.setdefault(ref, value)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    lines = ["## Evidence cited by the producer"]
    event = context["event"]
    binding_refs = {event["id"], *(event.get("source_refs") or ())}
    for ref in citations:
        item = found.get(ref)
        if item is None and ref not in binding_refs:
            raise EventJudgementValidationError(
                f"cited evidence {ref!r} is absent from the canonical verifier context"
            )
        lines.append(
            f"- {ref}: "
            + (json.dumps(item, ensure_ascii=False, sort_keys=True)
               if item is not None
               else json.dumps({
                   "id": event["id"], "kind": event.get("kind"),
                   "evidence_tier": event.get("evidence_tier"),
                   "occurred_at": event.get("occurred_at"),
                   "payload": {
                       key: value for key, value in (event.get("payload") or {}).items()
                       if value is not None and key not in {
                           "artifact_hash", "excerpt_hash", "invocation_ref"
                       }
                   },
                   "source_refs": event.get("source_refs") or [],
               }, ensure_ascii=False, sort_keys=True))
        )
    return lines


def _producer_draft_line(label: str, record: Mapping[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key not in {"model", "status"}}
    return f"{label} (canonical JSON): {json.dumps(body, ensure_ascii=False, sort_keys=True)}"


def _event_group_lines(context: Mapping[str, Any]) -> list[str]:
    rows = [context["event"], *(context.get("grouped_events") or [])]
    unique = {row["id"]: row for row in rows}
    return [
        "## Complete event evidence reviewed",
        *(json.dumps(row, ensure_ascii=False, sort_keys=True)
          for row in unique.values()),
    ]


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


#: The evidence-kind vocabulary as prompt lines.  Built once at import: the
#: definitions are frozen constants and rebuilding them per call would put the
#: same bytes through the same join on every judgement.
EVIDENCE_KIND_LINES: tuple[str, ...] = tuple(
    f"  {kind}: {EVIDENCE_KIND_DEFINITIONS[kind]}" for kind in EVIDENCE_KINDS
)


def _claim_source(event: Mapping[str, Any]) -> str | None:
    parts = str((event.get("payload") or {}).get("claim_ref") or "").split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "claim" else None


def _grouped_input_lines(context: Mapping[str, Any], *, heading: str) -> list[str]:
    """The other members of the event's group, as the judge and verifier read them.

    A buyback group is one filing's monthly rows or one closed HK week and is
    printed as the table it is.  The Claims drawn from one document
    (``context["grouped_by_document"]``) are printed in full under an
    instruction that any single one can change the decision.  Any other group
    is a batch of low-tier inputs on one company and day, or one document found
    twice: every input is printed in full, and the instruction says the batch
    is not a vote -- one input that matters is the decision.
    """

    event = context["event"]
    grouped = context.get("grouped_events") or ()
    if len(grouped) <= 1:
        return []
    if context.get("grouped_by_document"):
        # 2026-09-25: the Claims of one document share a call.  Every decision
        # other than NO_CHANGE so far was one public_web Claim among its
        # document's many (ACN: bookings down, a risk factor), so the judge and
        # the verifier are told, before they read them, that each stands alone.
        sources = {_claim_source(row) for row in grouped}
        lines = [
            "", f"{heading}Other Claims drawn from the same document (same judgement)"
            + ("" if heading else ":"),
            "These Claims were extracted from the same source document as this one.",
            "Each is a separate finding and every one was read in full. Judge each",
            "Claim on its own: ANY SINGLE Claim can change the decision by itself.",
            "If any one of them warrants a decision other than NO_CHANGE, the decision",
            "is taken on that Claim and cites its ref. How many of the others say",
            "nothing new does not matter; they do not dilute it, and the batch is",
            "not a vote.",
        ]
        if "public_web" in sources:
            lines.extend([
                "Claims from public web pages on the company's own results and risk",
                "disclosures (bookings, orders, risk factors) have moved views here",
                "before: read each of them as if it had arrived alone.",
            ])
        for row in grouped[1:]:
            lines.append(
                f"- ref: {row['id']} ({row.get('kind')}, evidence tier: "
                f"{row.get('evidence_tier')}, occurred_at: {row.get('occurred_at')})"
            )
            lines.extend(f"  {line}" for line in _payload_lines(row))
            lines.append(f"    source refs: {', '.join(row.get('source_refs') or ())}")
        return lines
    if event.get("kind") != "buyback_disclosure":
        lines = [
            "", f"{heading}Other inputs judged together with this one (same judgement)"
            + ("" if heading else ":"),
            "These are further inputs on this company from the same day, or the same",
            "document found again. Every one was read: the decision covers all of them.",
            "If any single input warrants a decision other than NO_CHANGE, the decision",
            "is taken on that input and cites its ref; the others do not dilute it.",
        ]
        for row in grouped[1:]:
            lines.append(
                f"- ref: {row['id']} ({row.get('kind')}, evidence tier: "
                f"{row.get('evidence_tier')}, occurred_at: {row.get('occurred_at')})"
            )
            lines.extend(f"  {line}" for line in _payload_lines(row))
            lines.append(f"    source refs: {', '.join(row.get('source_refs') or ())}")
        return lines
    market = ((event.get("payload") or {}).get("market"))
    label = ("HK daily rows in this closed ISO week (same judgement)"
             if market == "HK"
             else "Other monthly rows in this filing (same judgement)")
    lines = ["", f"{heading}{label}" + ("" if heading else ":")]
    for row in grouped[1:]:
        payload = row.get("payload") or {}
        lines.append(
            f"- ref: {row['id']}; period_label={payload.get('period_label')}; "
            f"period_end={payload.get('period_end')}; "
            f"shares_purchased={payload.get('shares_purchased')}; "
            f"average_price_paid={payload.get('average_price_paid')}; "
            f"total_paid={payload.get('total_paid')}; "
            f"shares_purchased_under_plans="
            f"{payload.get('shares_purchased_under_plans')}; "
            f"remaining_authorisation={payload.get('remaining_authorisation')}"
        )
    return lines


def build_judge_prompt(
    context: Mapping[str, Any], *, max_prompt_bytes: int = MAX_PROMPT_BYTES
) -> str:
    """The whole table the brain reads: event, theses, drivers, history, sources.

    A table rather than a narrative because every line of it has a ref the
    answer may cite, and a narrative would let the model cite the narrative.
    """

    event = context["event"]
    lines = [
        "You are the covering analyst for this company. One thing happened. Decide what,",
        "if anything, should change because of it. Deciding that nothing should change is a",
        "real answer and is often the right one -- but you must say why.",
        *final_text_instructions(),
        "",
        "Weigh the event by its evidence tier. A company-filed number and a crowd post are",
        "not the same kind of fact; a sales note is what the desk heard, not what happened.",
        "",
        "Weigh it also by what kind of quantity it is. These are the kinds this system",
        "records, and they are not interchangeable:",
        *EVIDENCE_KIND_LINES,
        "A market_proxy is never the company's own number. If a driver or an assumption",
        "below rests on one, the distance from what the company realises is written",
        "beside it, and a decision that treats the proxy as the realised figure is wrong",
        "however good the proxy is.",
        "",
        "In `because`, connect the event to business fundamentals and the existing",
        "driver model. Distinguish earnings delivery, expectation revisions and valuation",
        "changes where the supplied evidence supports them. Agreement with the market",
        "does not erase investment value; do not manufacture a disagreement. If our view",
        "does differ, name the assumption and observable that could resolve it. If the",
        "event is a price_divergence, the price has been running against our thesis: say",
        "what we may have missed rather than restating the thesis.",
        "State the current preferred interpretation explicitly. Explain the conditions",
        "under which it or the alternative would hold, their effects, and what new",
        "observation would make you change your decision. 'Both may happen' is not a",
        "judgement. Be decisive within the evidence; do not invent facts or probabilities.",
        "",
        f"## The event ({event['kind']}, evidence tier: {event['evidence_tier']})",
        f"ref: {event['id']}",
        f"occurred_at: {event['occurred_at']}",
        *_payload_lines(event),
        f"source refs: {', '.join(event['source_refs'])}",
        # W4: the arithmetic that turns a filing into something readable --
        # what fraction of a director's holding they just sold, what the
        # company paid for its own shares against what they cost now. All of
        # it is derived from filings this Core holds, none of it is a
        # judgement, and it is printed here rather than left to the model to
        # infer from a payload because a model asked to divide two numbers in
        # its head will sometimes divide them wrong and never say so.
        *(context.get("derived_lines") or ()),
        "",
        f"## Company: {context.get('ticker') or context['company_ref']} ({context['company_ref']})",
    ]
    lines.extend(_grouped_input_lines(context, heading="## "))
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
            for gap in assumption.get("proxy_gaps") or ():
                # The one thing that must travel with a proxy: without it the
                # judge reads a spread as the company's realised margin.
                lines.append(f"      market_proxy gap: {gap}")
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
    material = context.get("result")
    if material:
        # Batch F: the event's own body, the same-event pack, the expectations
        # baseline and the market reaction, every figure tagged; and, for a
        # result-type event, the 实际 vs 预期 contract.
        from .event_judgement_result import prompt_lines as result_prompt_lines

        lines.extend(result_prompt_lines(material))
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
        'If the value is outside this company\'s filed range, also add '
        '"outside_band_reason": "<one sentence explaining why the filed range no longer constrains it>" '
        'inside forecast_change; otherwise publication will be refused.'
    )
    lines.append(
        'Add "research_question": "<one question>" if and only if action is "research".'
    )
    if material:
        from .event_judgement_result import output_spec_lines

        lines.extend(output_spec_lines(material))
    lines.extend([
        f"Reference-list limits: driver_refs, thesis_refs and citations must each be "
        f"a JSON list of at most {MAX_REFS} references (count list entries before deduplication). "
        "For citations, choose the decisive references actually supporting the judgement; "
        "do not copy every input or numbered tag into this top-level list.",
        "For a result event, retain every required numeric source tag in the corresponding "
        "actual_vs_expected row's sources and figure text. These per-row source bindings "
        "remain required even when that tag is not selected among the top-level citations. "
        "Do not discard a figure's evidence to fit the top-level list limit.",
        f"Text limits are characters, not words: because, forecast_change.because and "
        f"forecast_change.outside_band_reason each allow at most {MAX_BECAUSE_CHARS}; "
        f"note at most {MAX_NOTE_CHARS} characters and {MAX_NOTE_SENTENCES} sentences; "
        "research_question at most 500 characters.",
        "Use the available space for the decision, decisive evidence and the specific "
        "condition that would change it. Do not repeat the source table or write a full memo.",
    ])
    lines.append('For revise_thesis, add proposed_statement (a complete nonempty revised thesis) and '
                 'proposed_confidence (low|medium|high, consistent with its wording). Name the thesis version. '
                 'A statement opposite to the old position requires THESIS_BROKEN, not THESIS_WEAKENED.')
    lines.append("Every citation must be a ref printed above. Do not invent a ref.")
    prompt = "\n".join(lines)
    return _bounded_prompt(prompt, max_bytes=max_prompt_bytes)


def allowed_refs(context: Mapping[str, Any]) -> set[str]:
    """Every ref the prompt printed.  A citation outside this set is a refusal."""

    refs = {context["event"]["id"], *context["event"]["source_refs"]}
    for grouped in context.get("grouped_events") or ():
        refs.add(grouped["id"])
        refs.update(grouped.get("source_refs") or ())
        for field in REF_PAYLOAD_FIELDS:
            value = (grouped.get("payload") or {}).get(field)
            if isinstance(value, str) and value:
                refs.add(value)
    for field in REF_PAYLOAD_FIELDS:
        value = context["event"]["payload"].get(field)
        if isinstance(value, str) and value:
            refs.add(value)
    refs.update(str(ref) for ref in (context.get("derived_refs") or ()) if ref)
    refs.update(thesis["ref"] for thesis in context.get("theses") or ())
    refs.update(driver["ref"] for driver in context.get("drivers") or ())
    refs.update(claim["ref"] for claim in context.get("claims") or ())
    material = context.get("result") or {}
    for tag, ref in (material.get("tags") or {}).items():
        # A tag ([P3] / N12) and the passage or pack number it binds are both citable.
        if not str(tag).startswith(("O", "C")):
            refs.add(str(tag))
            refs.add(f"[{tag}]")
            refs.add(str(ref))
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
    if action == "revise_thesis":
        expected |= {"proposed_statement", "proposed_confidence"}
    material = context.get("result") or {}
    result_contract = material.get("kind") == "result"
    if result_contract:
        from .event_judgement_result import RESULT_KEYS

        expected |= set(RESULT_KEYS)
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
    normalised_result = None
    if result_contract:
        from .event_judgement_result import result_contract_problems

        normalised_result, problems = result_contract_problems(
            value, material, decision=decision, action=action)
        if problems:
            raise EventJudgementValidationError(
                "the result-event contract is not met: " + "; ".join(problems[:6]))
    elif material and not material.get("body_read") and not material.get("group_readable"):
        # Batch F: a document whose body was not read is never judged on its
        # title.  Only 证据不足，安排读取 is an answer (the lane records that one
        # deterministically; this is the refusal for a model that tried more).
        from .event_judgement_result import body_required

        if body_required(context["event"]) and (decision, action) != ("NO_CHANGE", "research"):
            raise EventJudgementValidationError(
                "the event's body was not read: the only answer is NO_CHANGE / research "
                "(证据不足，安排读取), not " + f"{decision} / {action}")
    result: dict[str, Any] = {
        "decision": decision,
        "action": action,
        "driver_refs": driver_refs,
        "thesis_refs": thesis_refs,
        "because": _text(value["because"], "because", maximum=MAX_BECAUSE_CHARS),
        "citations": citations,
    }
    if action == "revise_thesis":
        from .thesis_question_model import validate_revision_proposal
        try:
            validate_revision_proposal(value["proposed_statement"], value["proposed_confidence"], decision=decision)
        except ValueError as exc:
            raise EventJudgementValidationError(str(exc)) from exc
        result["proposed_statement"] = _text(value["proposed_statement"], "proposed_statement", maximum=4000)
        result["proposed_confidence"] = value["proposed_confidence"]
        if not thesis_refs:
            raise EventJudgementValidationError("revise_thesis must name the current thesis")
    if normalised_result is not None:
        result["result"] = normalised_result
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
        base_fields = {"driver_ref", "period_end", "value", "because"}
        if not isinstance(change, Mapping) or set(change) not in (
            base_fields, base_fields | {"outside_band_reason"},
        ):
            raise EventJudgementValidationError(
                "forecast_change must contain driver_ref, period_end, value and because, "
                "with optional outside_band_reason"
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
        if "outside_band_reason" in change:
            reason = _text(change["outside_band_reason"],
                           "forecast_change.outside_band_reason",
                           maximum=MAX_BECAUSE_CHARS)
            if len(reason.split()) < 3:
                raise EventJudgementValidationError(
                    "forecast_change.outside_band_reason must contain at least 3 words"
                )
            result["forecast_change"]["outside_band_reason"] = reason
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
    context: Mapping[str, Any], judgement: Mapping[str, Any],
    *, max_prompt_bytes: int = MAX_PROMPT_BYTES,
) -> str:
    """The self-reflection call: what we expected, what happened, what we missed."""

    event = context["event"]
    market = context.get("market_view") or ()
    lines = [
        "You are the covering analyst writing down what you got wrong, or what you are",
        "holding through and why. This is not a defence of the thesis and it changes",
        "nothing: a person reads it beside the revision candidate.",
        *final_text_instructions(),
        "",
        "Compare what we expected with the new evidence, preserving the earlier view",
        "as it was. Explain whether the evidence changes a business driver, an earnings",
        "expectation, valuation or only market attention. Identify alternative explanations,",
        "counterevidence and the next observable check. Agreement with the market can",
        "still support earnings delivery or valuation normalization; do not force a variant",
        "view or invent numerical probabilities from an uncalibrated impression.",
        "Choose a current main case. For that case and its material alternative, say",
        "what conditions trigger it and what changes for the business, expectations",
        "or valuation. Put the discriminating observation, source and review cadence",
        "into followup_tracking where the supplied source table supports it; use",
        "followup_research for unresolved assumptions. These remain proposals until",
        "the existing authority actually admits them; never claim a task was scheduled.",
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
            "(no market-view evidence is supplied in this context. This does not prove "
            "the system has no consensus capability or no other stored material. Do not infer a "
            "street view from the absence of one.)"
        )
    lines.append("")
    lines.append("## What this fund itself said about this company before")
    prior = list(context.get("prior_view") or ())
    if prior:
        for row in prior:
            lines.append(
                f"- {row['refs'][0] if row.get('refs') else '?'} "
                f"({row.get('source_kind')}, as of {row.get('as_of')}): "
                f"{row.get('statement')}"
            )
        lines.append(
            "This is our own earlier view, not evidence. Say whether it still holds, "
            "has changed, or has been answered, and cite the refs printed above for "
            "whichever you say. Do not restate it as though it were today's view."
        )
    else:
        lines.append("(nothing: this company has no earlier internal file on this Core.)")
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
    if prior:
        lines.append(
            'Add one more key: "prior_view": {"as_of": "YYYY-MM-DD", "still_holds": '
            '"still_holds|changed|resolved", "summary": "...", "refs": []}. as_of is the '
            "date printed beside the prior view above, never today's date. Omit the key "
            "entirely if you cannot cite the prior material."
        )
    lines.append(
        "followup_tracking names a source_key from the table above and a proposed interval "
        "in seconds; followup_research names a question. Both are proposals a later step "
        "decides on -- nothing you write here changes a cadence or opens a task."
    )
    lines.append("Every citation must be a ref printed above. Do not invent a ref.")
    return _bounded_prompt("\n".join(lines), max_bytes=max_prompt_bytes)


def validate_reflection_output(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape.  Refused whole; a reflection is not repaired either."""

    if not isinstance(value, Mapping):
        raise EventJudgementValidationError("the model did not return an object")
    expected = {
        "thesis_refs", "what_we_expected", "what_happened", "why", "citations",
        "missed_debates", "followup_tracking", "followup_research",
        "market_view_vs_ours", "convergence_pathway",
    }
    # W3: what *we* said about this company before, with the date on it. Beside
    # `market_view_vs_ours` and not inside it: the street's position and our
    # own earlier position are two different things to disagree with, and
    # folding them together loses which one is being argued with. Optional
    # rather than required, because most companies have no prior file and a
    # required key would force the model to write "none" for the majority.
    optional = {"prior_view"}
    extra = sorted(set(value) - expected - optional)
    if extra:
        raise EventJudgementValidationError(
            f"the reflection returned keys the contract does not have: {extra}"
        )
    missing = sorted(expected - set(value))
    if missing:
        raise EventJudgementValidationError(f"the reflection omitted required keys: {missing}")
    permitted = allowed_refs(context) | {
        row["ref"] for row in (context.get("market_view") or ())
    } | {
        ref
        for row in (context.get("prior_view") or ())
        for ref in (row.get("refs") or ())
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
        **({} if "prior_view" not in value else {
            "prior_view": _prior_view_answer(value["prior_view"], permitted)
        }),
    }


def _prior_view_answer(value: Any, permitted: set[str]) -> dict[str, Any]:
    """The model's reading of what we thought before, dated and cited.

    ``as_of`` is required and is not defaulted to today. The one thing a prior
    view has to carry is its age; a prior view stamped with the day it was read
    is a prior view that will be argued with as though it were current.
    """

    if not isinstance(value, Mapping) or set(value) != {
        "as_of", "still_holds", "summary", "refs"
    }:
        raise EventJudgementValidationError(
            "prior_view must be exactly as_of, still_holds, summary and refs"
        )
    as_of = _text(value["as_of"], "prior_view.as_of", maximum=10)
    if _PRIOR_AS_OF_RE.fullmatch(as_of) is None:
        raise EventJudgementValidationError("prior_view.as_of must be YYYY-MM-DD")
    if value["still_holds"] not in PRIOR_VIEW_VERDICTS:
        raise EventJudgementValidationError(
            f"prior_view.still_holds must be one of {list(PRIOR_VIEW_VERDICTS)}"
        )
    refs = _ref_list(value["refs"], "prior_view.refs")
    stray = sorted(set(refs) - permitted)
    if stray:
        raise EventJudgementValidationError(
            f"prior_view cites refs it was not shown: {stray}"
        )
    if not refs:
        raise EventJudgementValidationError(
            "a prior_view must cite the prior material it is reading"
        )
    return {
        "as_of": as_of,
        "still_holds": value["still_holds"],
        "summary": _text(value["summary"], "prior_view.summary",
                         maximum=MAX_BECAUSE_CHARS),
        "refs": refs,
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

    try:
        prompt = build_reflection_prompt(
            context, judgement,
            max_prompt_bytes=_model_prompt_bound(model, REFLECTION_PURPOSE),
        )
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": None}
    try:
        call = model.call(
            purpose=REFLECTION_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the reflection call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "failure_trace": model_failure_trace(exc), "model": None}
    provenance = _provenance(call)
    provenance["purpose"] = REFLECTION_PURPOSE
    try:
        validated = validate_reflection_output(unwrap_json_object(call["text"]), context)
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {"status": "reflected", "model": provenance, **validated}


def build_reflection_verifier_prompt(
    context: Mapping[str, Any], reflection: Mapping[str, Any],
    *, max_prompt_bytes: int = MAX_PROMPT_BYTES,
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
        _producer_draft_line("Producer reflection", reflection),
        *_event_group_lines(context),
        *_cited_evidence_lines(context, reflection["citations"]),
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
    ]
    return _bounded_prompt(
        "\n".join(lines), preserve_from="Producer reflection (canonical JSON):",
        max_bytes=max_prompt_bytes,
    )


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
    try:
        prompt = build_reflection_verifier_prompt(
            context, reflection,
            max_prompt_bytes=_model_prompt_bound(model, REFLECTION_VERIFIER_PURPOSE),
        )
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": None}
    try:
        call = independent_model_call(
            model,
            producer_route_decision_refs=[
                (reflection.get("model") or {}).get("route_decision_ref")
            ],
            purpose=REFLECTION_VERIFIER_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "model": None,
                "lane_status": lane_status_for(exc, "refused"),
                "failure_trace": model_failure_trace(exc),
                "reason": f"the reflection verifier call did not succeed: {exc}"}
    provenance = _provenance(call)
    provenance["purpose"] = REFLECTION_VERIFIER_PURPOSE
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


def build_verifier_prompt(
    context: Mapping[str, Any], judgement: Mapping[str, Any],
    *, max_prompt_bytes: int = MAX_PROMPT_BYTES,
) -> str:
    from .research_reasoning_lint import VERIFIER_LINT_INSTRUCTIONS

    event = context["event"]
    lines = [
        "You are an independent verifier. Another model read one event and decided what,",
        "if anything, should change. You do not re-decide and you do not improve it. You",
        "answer one question: does the decision follow from what the event actually says,",
        "at the weight its evidence tier deserves?",
        "A supported no-change decision or agreement with the market is valid. Reject",
        "unsupported causal attribution or a proxy presented as a fact, not the absence",
        "of contrarianism. Confidence changes must follow the cited new evidence.",
        "Check that the stated decision has a preferred interpretation and concrete",
        "conditions or observations that could change it, not uncommitted alternatives.",
        "Research reasoning rules (report a broken rule with an existing evidence finding code):",
        *VERIFIER_LINT_INSTRUCTIONS,
        "",
        f"Event ({event['kind']}, evidence tier {event['evidence_tier']}, ref {event['id']}):",
        *_payload_lines(event),
    ]
    lines.extend(_grouped_input_lines(context, heading=""))
    lines.extend([
        "",
        f"Decision: {judgement['decision']} / {judgement['action']}",
        *thesis_direction_prompt(context, judgement),
        f"Because: {judgement['because']}",
        f"Cited: {', '.join(judgement['citations']) or '(nothing)'}",
        f"Drivers named: {', '.join(judgement['driver_refs']) or '(none)'}",
        f"Theses named: {', '.join(judgement['thesis_refs']) or '(none)'}",
        "",
        *_result_verifier_lines(context, judgement),
        _producer_draft_line("Producer judgement", judgement),
        *_event_group_lines(context),
        *_cited_evidence_lines(context, judgement["citations"]),
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"code": "'
        + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
    ])
    return _bounded_prompt(
        "\n".join(lines), preserve_from="Producer judgement (canonical JSON):",
        max_bytes=max_prompt_bytes,
    )


def _result_verifier_lines(context: Mapping[str, Any], judgement: Mapping[str, Any]) -> list[str]:
    """Batch F: what the verifier checks on a result-type event's table."""

    result = judgement.get("result")
    material = context.get("result") or {}
    if not result:
        return []
    texts = material.get("tag_texts") or {}
    cited = sorted({tag for row in result.get("actual_vs_expected") or () for tag in row.get("sources") or ()})
    lines = [
        "",
        "This is a result-type event. The producer filled an 实际 vs 预期 table; check that each row's",
        "figures are what its tagged sources say, that beat / miss / range words follow from the figures,",
        "that the market reaction is read against the stated benchmark (or says 无基准), and that a",
        "NO_CHANGE explains why the surprise does not change the thesis.",
        f"Evidence status: {result.get('evidence_status')}; event body read: {bool(material.get('body_read'))}",
        "Table (canonical JSON): " + json.dumps(result.get("actual_vs_expected") or [], ensure_ascii=False,
                                                 sort_keys=True),
        "Market reaction: " + json.dumps(result.get("market_reaction") or {}, ensure_ascii=False, sort_keys=True),
        "Thesis impact: " + json.dumps(result.get("thesis_impact") or {}, ensure_ascii=False, sort_keys=True),
        "## The tagged sources the table cites",
    ]
    for tag in cited:
        lines.append(f"[{tag}] {str(texts.get(tag, ''))[:900]}")
    return lines


def thesis_direction_proposals(context, judgement):
    from .thesis_question_model import direction_binding
    if not judgement.get("proposed_statement"):
        return []
    named = set(judgement.get("thesis_refs") or ())
    return [direction_binding(t, judgement["proposed_statement"], judgement["proposed_confidence"])
            for t in context.get("theses") or () if t["ref"] in named]


def thesis_direction_prompt(context, judgement):
    if not judgement.get("proposed_statement"):
        return []
    return ["Compare each named old thesis with the proposed statement, including confidence wording. "
            "Add direction_checks to the JSON: one {thesis_version_ref, relation: same|opposite|unclear, reason} "
            "in the order below. Opposite direction requires THESIS_BROKEN; unclear cannot pass. "
            "Do not trust the producer's direction label.",
            "Old theses: " + canonical_json(list(context.get("theses") or ())),
            "Proposal: " + canonical_json({"statement": judgement["proposed_statement"], "confidence": judgement["proposed_confidence"]}),
            "Required checks: " + canonical_json(thesis_direction_proposals(context, judgement))]


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


def _model_prompt_bound(model: Any, purpose: str) -> int:
    resolver = getattr(model, "budget_for", None)
    if callable(resolver):
        return int(resolver(purpose)["max_input_tokens"])
    return MAX_PROMPT_BYTES


def judge(
    context: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    prompt_sink: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """One bounded call, verified against the closed schema before it is believed.

    ``prompt_sink(role, prompt)`` receives the exact prompt sent, so the run
    directory keeps it for audit (batch F).
    """

    try:
        prompt = build_judge_prompt(
            context, max_prompt_bytes=_model_prompt_bound(model, PURPOSE)
        )
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "prompt_chars": 0,
                "model": None}
    if prompt_sink is not None:
        prompt_sink("judge", prompt)
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
                "prompt_chars": len(prompt),
                "failure_trace": model_failure_trace(exc), "model": None}
    provenance = _provenance(call)
    try:
        validated = validate_judge_output(unwrap_json_object(call["text"]), context)
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc),
                "prompt_chars": len(prompt), "model": provenance}
    return {"status": "judged", "prompt_chars": len(prompt), "model": provenance, **validated}


def reasoning_findings(context: Mapping[str, Any], judgement: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Lint the proposed analysis, excluding quoted sources and research questions."""
    from .research_reasoning_lint import lint_text, source_kinds_for_refs

    material = context.get("result") or {}
    aliases = dict(material.get("tags") or {})
    refs = list(judgement.get("citations") or ())
    for row in (judgement.get("result") or {}).get("actual_vs_expected") or ():
        refs.extend(row.get("sources") or ())
    consensus_ref = material.get("consensus_version_ref")
    if isinstance(consensus_ref, str) and consensus_ref.startswith(("consensus-estimate-version:", "consensus-estimate:")):
        for tag, ref in list(aliases.items()):
            if str(ref).startswith("pack-json:/event_result/consensus/"):
                aliases[tag] = consensus_ref
    kinds = source_kinds_for_refs(refs, aliases)
    sections = {key: judgement[key] for key in ("because", "note") if judgement.get(key)}
    change = judgement.get("forecast_change") or {}
    if change.get("because"):
        sections["forecast_change"] = change["because"]
    result = judgement.get("result") or {}
    for key in ("thesis_impact", "market_reaction"):
        value = result.get(key)
        if isinstance(value, str):
            sections[key] = value
        elif isinstance(value, Mapping):
            sections[key] = "\n".join(str(value[name]) for name in
                ("because", "reason", "interpretation", "statement", "explanation") if value.get(name))
    return [finding for name, text in sections.items()
            for finding in lint_text(str(text), section=name, source_kinds=kinds)]


def verify(
    context: Mapping[str, Any],
    judgement: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
    family_resolver: Callable[[str | None], str | None],
    prompt_sink: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the first.

    Fails closed on independence.  If the two families cannot be resolved, or
    resolve to the same family, the verification is refused rather than
    recorded as a pass: a verifier that might be the producer is not one.
    """

    if judgement.get("status") != "judged":
        return {"status": "skipped", "reason": "there is no judgement to verify"}
    lint = reasoning_findings(context, judgement)
    if lint:
        return {"status": "refused", "model": None, "reason": "reasoning_lint",
                "reasoning_findings": lint, "findings": lint}
    # Resolve what can be resolved before paying: a producer whose family is
    # unknown can never be verified independently, so there is nothing to buy.
    producer_family = family_resolver((judgement.get("model") or {}).get("route_decision_ref"))
    if producer_family is None:
        return {"status": "refused", "model": None,
                "independence": {"producer_family": None, "verifier_family": None,
                                 "predicate": "model_family_ne"},
                "reason": "the model family behind the judgement could not be resolved; "
                          "an unverifiable independence claim is not independence"}
    try:
        prompt = build_verifier_prompt(
            context, judgement,
            max_prompt_bytes=_model_prompt_bound(model, VERIFIER_PURPOSE),
        )
    except EventJudgementValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": None}
    if prompt_sink is not None:
        prompt_sink("verifier", prompt)
    try:
        call = independent_model_call(
            model,
            producer_route_decision_refs=[
                (judgement.get("model") or {}).get("route_decision_ref")
            ],
            purpose=VERIFIER_PURPOSE, request_id=request_id, prompt=prompt, mission=mission
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"the verifier call did not succeed: {exc}",
                "lane_status": lane_status_for(exc, "refused"),
                "failure_trace": model_failure_trace(exc), "model": None}
    provenance = _provenance(call)
    provenance["purpose"] = VERIFIER_PURPOSE
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
        payload = unwrap_json_object(call["text"])
        checks = payload.pop("direction_checks", None)
        validated = validate_verifier_output(payload)
        proposals = thesis_direction_proposals(context, judgement)
        if proposals:
            from .thesis_question_model import validate_directions
            validated["direction_checks"] = validate_directions(checks, proposals)
            if any(c["relation"] == "unclear" or (c["relation"] == "opposite" and judgement["decision"] != "THESIS_BROKEN") for c in validated["direction_checks"]):
                raise EventJudgementValidationError("opposite thesis requires THESIS_BROKEN; unclear direction requires review")

    except (EventJudgementValidationError, ValueError) as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance,
                "independence": independence}
    from .research_product_quality import product_quality
    quality = product_quality("event_judgement", judgement,
                              sources=(context.get("result") or {}).get("tags") or {})
    return {
        "status": "verified", "quality": quality,
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

    @classmethod
    def reader(cls, connection: sqlite3.Connection) -> "EventJudgementAuthority":
        """Reads only, over a connection this does not own: no schema, no writes.

        For an owner's dry run against a Core opened ``mode=ro``; every
        writing method on the result fails because it has no store.
        """

        authority = cls.__new__(cls)
        authority.store = None
        authority.connection = connection
        return authority

    # -- reading -----------------------------------------------------------

    def judgement_for(self, event_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM event_judgements WHERE event_ref=?",
            (_text(event_ref, "event_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "EventJudgement")

    def latest_judgement_for(self, event_ref: str) -> dict[str, Any] | None:
        """The newest judgement of an event: its last re-judgement, else the original."""

        row = self.connection.execute(
            "SELECT * FROM event_judgements WHERE event_ref=? OR event_ref LIKE ? "
            "ORDER BY created_at DESC, judgement_id DESC LIMIT 1",
            (_text(event_ref, "event_ref"), event_ref + "#rejudgement-%"),
        ).fetchone()
        return None if row is None else _decode(row, "EventJudgement")

    def rejudgements_for(self, event_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM event_judgements WHERE event_ref LIKE ? ORDER BY created_at, judgement_id",
            (_text(event_ref, "event_ref") + "#rejudgement-%",),
        ).fetchall()
        return [_decode(row, "EventJudgement") for row in rows]

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
        annotations: Mapping[str, Any] | None = None,
        rejudgement: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind one decision to one event and one pair of model work orders.

        ``annotations`` (batch F) carry what the judge read and when the event
        happened: ``result_material`` (pack record, documents read, tag map,
        dates, main event) and ``event_dates``.  ``rejudgement`` makes this a
        bounded re-judgement of an already judged event: it is stored under
        ``<event>#rejudgement-<n>`` (n at most ``MAX_REJUDGEMENTS``), with the
        reason and the judgement it follows.
        """

        actor_ref = _text(actor_ref, "actor_ref")
        if actor_ref.startswith("automation:") and (
            actor_ref != mission["autonomy"]["automation_principal"]
        ):
            raise EventJudgementConflict("automation actor is not the mission principal")
        event_ref = event["id"]
        if rejudgement is not None:
            from .event_judgement_result import MAX_REJUDGEMENTS, rejudgement_ref

            sequence = int(rejudgement.get("sequence") or 0)
            if not 1 <= sequence <= MAX_REJUDGEMENTS:
                raise EventJudgementValidationError(
                    f"a re-judgement is numbered 1..{MAX_REJUDGEMENTS}, got {sequence}")
            prior = self.connection.execute(
                "SELECT COUNT(*) FROM event_judgements WHERE event_ref=? OR event_ref LIKE ?",
                (event["id"], event["id"] + "#rejudgement-%")).fetchone()[0]
            if int(prior) != sequence:
                existing = self.connection.execute(
                    "SELECT * FROM event_judgements WHERE event_ref=?",
                    (rejudgement_ref(event["id"], sequence),)).fetchone()
                if existing is not None:
                    return {**_decode(existing, "EventJudgement"), "status": "duplicate"}
                raise EventJudgementConflict(
                    f"re-judgement {sequence} of {event['id']} does not follow its {int(prior)} "
                    "earlier judgement(s)")
            event_ref = rejudgement_ref(event["id"], sequence)
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "event-judgement:" + content_hash({
                "event_ref": event_ref,
                "event_hash": event["content_hash"],
                "work_order_ref": (judgement.get("model") or {}).get("work_order_ref"),
            })[:32],
            "created_at": _now(),
            "event_ref": event_ref,
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
            "proposed_statement": judgement.get("proposed_statement"),
            "proposed_confidence": judgement.get("proposed_confidence"),
            "verifier": {
                "status": verification.get("status"),
                "verdict": verification.get("verdict"),
                "findings": list(verification.get("findings") or ()),
                "independence": verification.get("independence"),
                "reason": verification.get("reason"),
                "direction_checks": verification.get("direction_checks"),
            },
            "model": judgement.get("model"),
            "verifier_model": verification.get("model"),
            "effect": dict(effect),
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        if judgement.get("result") is not None:
            record["result"] = dict(judgement["result"])
        for key, value in (annotations or {}).items():
            if key in ("result_material", "event_dates", "main_event_ref") and value is not None:
                record[key] = value
        if rejudgement is not None:
            record["rejudgement"] = {
                "of_event_ref": event["id"], "sequence": int(rejudgement["sequence"]),
                "reason": str(rejudgement.get("reason")), "label": rejudgement.get("label"),
                "prior_judgement_ref": rejudgement.get("prior_judgement_ref"),
                "trigger_refs": list(rejudgement.get("trigger_refs") or ())[:20],
            }
        record["content_hash"] = content_hash(record)
        cost = int((judgement.get("model") or {}).get("cost_micros") or 0) + int(
            (verification.get("model") or {}).get("cost_micros") or 0
        )
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM event_judgements WHERE event_ref=?", (event_ref,)
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "EventJudgement"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO event_judgements(judgement_id,event_ref,event_hash,company_ref,"
                "decision,action,verdict,cost_micros,mission_version_ref,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], event_ref, event["content_hash"], event["company_ref"],
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
            # W3, absent rather than null when there was no prior file. Read
            # with ``.get`` because callers hand this function hand-written
            # reflections and a required key would break them; written with a
            # conditional because a stored ``None`` and an absent key are
            # different sentences to a reader of the ledger.
            **({} if reflection.get("prior_view") is None else {
                "prior_view": dict(reflection["prior_view"])
            }),
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
        from .thesis_question_model import validate_revision_proposal
        try:
            validate_revision_proposal(proposed_statement, proposed_confidence, decision=decision)
        except ValueError as exc:
            raise EventJudgementValidationError(str(exc)) from exc
        held = self.connection.execute("SELECT content_json FROM thesis_versions WHERE version_id=?", (thesis["id"],)).fetchone()
        if held:
            old = json.loads(held[0])
            if (proposed_statement is None or proposed_statement == old.get("statement")) and proposed_confidence == old.get("confidence"):
                raise EventJudgementValidationError("an unchanged thesis is not a revision candidate")
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

    def followup_notices(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM judgement_followup_notices"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at, notice_id"
        return [
            _decode(row, "JudgementFollowupNotice")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def followups_for(self, judgement_ref: str) -> dict[str, list[str]]:
        """Every governed follow-up already written for one judgement, by kind."""

        ref = _text(judgement_ref, "judgement_ref")
        return {
            "candidates": [row[0] for row in self.connection.execute(
                "SELECT candidate_id FROM thesis_revision_candidates WHERE judgement_ref=? "
                "ORDER BY candidate_id", (ref,)).fetchall()],
            "proposals": [row[0] for row in self.connection.execute(
                "SELECT proposal_id FROM forecast_revision_proposals WHERE judgement_ref=? "
                "ORDER BY proposal_id", (ref,)).fetchall()],
            "notices": [row[0] for row in self.connection.execute(
                "SELECT notice_id FROM judgement_followup_notices WHERE judgement_ref=?",
                (ref,)).fetchall()] if self._has_table("judgement_followup_notices") else [],
            "no_revision": [row[0] for row in self.connection.execute(
                "SELECT followup_id FROM judgement_no_revision_followups "
                "WHERE judgement_ref=?", (ref,)).fetchall()]
            if self._has_table("judgement_no_revision_followups") else [],
        }

    def no_revision_followups(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        if not self._has_table("judgement_no_revision_followups"):
            return []
        query = "SELECT * FROM judgement_no_revision_followups"
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE company_ref=?"
            params.append(company_ref)
        query += " ORDER BY created_at, followup_id"
        return [
            _decode(row, "JudgementNoRevisionFollowup")
            for row in self.connection.execute(query, params).fetchall()
        ]

    def record_no_revision_followup(
        self,
        *,
        judgement_ref: str,
        company_ref: str,
        decision: str,
        action: str,
        because: str,
        evidence_refs: Sequence[str],
        maintained_theses: Sequence[Mapping[str, Any]],
        mission: Mapping[str, Any],
        actor_ref: str,
        reflection_ref: str | None = None,
    ) -> dict[str, Any]:
        """The governed record of "the preferred view keeps the current thesis".

        A judgement that names current, admitted thesis versions but proposes
        neither a new statement nor a new confidence has nothing a person
        could accept: a candidate written for it offered 「接受」 and the
        thesis authority refused it, correctly, as a rewrite of an unchanged
        view (ADR-0008).  It is recorded here instead -- one row per
        judgement, keyed on it, so the forward path and the backfill write it
        once -- and it is listed in the weekly brief.  It decides nothing.
        """

        if decision not in DECISION_VOCABULARY or decision == "NO_CHANGE":
            raise EventJudgementValidationError(
                "only a decision other than NO_CHANGE owes a follow-up"
            )
        refs = [_text(ref, "evidence_refs[]") for ref in (evidence_refs or ())]
        if not refs:
            raise EventJudgementValidationError("a follow-up must name its evidence")
        theses = []
        for thesis in maintained_theses:
            theses.append({
                "thesis_version_ref": _text(thesis.get("ref"), "thesis.ref"),
                "thesis_version_hash": thesis.get("content_hash"),
                "thesis_ref": thesis.get("thesis_ref"),
                "confidence": thesis.get("confidence"),
            })
        if not theses:
            raise EventJudgementValidationError(
                "a no-revision follow-up names the theses it keeps"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "judgement-no-revision-followup:" + content_hash({
                "judgement_ref": judgement_ref,
            })[:32],
            "created_at": _now(),
            "judgement_ref": judgement_ref,
            "company_ref": company_ref,
            "followup_kind": "no_revision_needed",
            "label": NO_REVISION_FOLLOWUP_LABEL,
            "decision": decision,
            "action": action,
            "because": _text(because, "because", maximum=MAX_BECAUSE_CHARS),
            "evidence_refs": list(dict.fromkeys(refs)),
            "maintained_theses": theses,
            "reflection_ref": reflection_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM judgement_no_revision_followups WHERE judgement_ref=?",
                (judgement_ref,),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "JudgementNoRevisionFollowup"),
                        "status": "duplicate"}
            cur.execute(
                "INSERT INTO judgement_no_revision_followups(followup_id,judgement_ref,"
                "company_ref,decision,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    record["id"], judgement_ref, company_ref, decision,
                    canonical_json(record), record["content_hash"], actor_ref,
                    record["created_at"],
                ),
            )
        return {**record, "status": "fresh"}

    def _has_table(self, name: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def record_followup_notice(
        self,
        *,
        judgement_ref: str,
        company_ref: str,
        notice_kind: str,
        decision: str,
        action: str,
        because: str,
        reason: str,
        evidence_refs: Sequence[str],
        named_thesis_refs: Sequence[str],
        admitted_thesis_refs: Sequence[str],
        model_version_ref: str | None,
        mission: Mapping[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        """The owner-facing notice for a judgement nothing else could carry.

        One per judgement, keyed on it, so a retry or a backfill that reaches
        the same judgement twice finds the first and writes nothing.
        """

        if notice_kind not in FOLLOWUP_NOTICE_KINDS:
            raise EventJudgementValidationError(
                f"a follow-up notice is one of {list(FOLLOWUP_NOTICE_KINDS)}"
            )
        if decision not in DECISION_VOCABULARY or decision == "NO_CHANGE":
            raise EventJudgementValidationError(
                "only a decision other than NO_CHANGE owes a follow-up"
            )
        refs = [_text(ref, "evidence_refs[]") for ref in (evidence_refs or ())]
        if not refs:
            raise EventJudgementValidationError("a follow-up notice must name its evidence")
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "judgement-followup-notice:" + content_hash({
                "judgement_ref": judgement_ref,
            })[:32],
            "created_at": _now(),
            "judgement_ref": judgement_ref,
            "company_ref": company_ref,
            "notice_kind": notice_kind,
            "label": FOLLOWUP_NOTICE_LABELS[notice_kind],
            "decision": decision,
            "action": action,
            "because": _text(because, "because", maximum=MAX_BECAUSE_CHARS),
            "reason": _text(reason, "reason", maximum=500),
            "evidence_refs": list(dict.fromkeys(refs)),
            "named_thesis_refs": list(dict.fromkeys(named_thesis_refs or ())),
            "admitted_thesis_refs": list(dict.fromkeys(admitted_thesis_refs or ())),
            "model_version_ref": model_version_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            existing = cur.execute(
                "SELECT * FROM judgement_followup_notices WHERE judgement_ref=?",
                (judgement_ref,),
            ).fetchone()
            if existing is not None:
                return {**_decode(existing, "JudgementFollowupNotice"), "status": "duplicate"}
            cur.execute(
                "INSERT INTO judgement_followup_notices(notice_id,judgement_ref,company_ref,"
                "notice_kind,decision,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], judgement_ref, company_ref, notice_kind, decision,
                    canonical_json(record), record["content_hash"], actor_ref,
                    record["created_at"],
                ),
            )
        return {**record, "status": "fresh"}


# ---------------------------------------------------------------------------
# the governed follow-up (ADR-0010): no judgement other than NO_CHANGE vanishes
# ---------------------------------------------------------------------------

#: Why a judgement left a notice instead of a candidate or a forecast change.
FOLLOWUP_NOTICE_KINDS: tuple[str, ...] = ("needs_thesis", "thesis_superseded")
FOLLOWUP_NOTICE_LABELS: Mapping[str, str] = MappingProxyType({
    "needs_thesis": "需要论点：这条判断改变了看法，但没有可绑定的已准入论点，"
                    "也没有可修订的公司模型；请在论点准入队列决定是否为它立论",
    "thesis_superseded": "论点已更新：这条判断针对的论点版本在它之后已由人修订，"
                         "已按资料变化阈值重新检查是否需要更新研究；达到阈值时重新开放复核",
})
#: The words the weekly brief shows for a judgement that keeps the thesis.
NO_REVISION_FOLLOWUP_LABEL = (
    "无需修订：这条判断首选维持现有论点，没有提出新的论点表述或置信度，"
    "已留档，不进入待决定队列"
)


def proposed_thesis_revision(
    judgement: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    """What the judgement itself proposes the thesis should now say.

    New revise_thesis decisions name a complete statement and explicit confidence.
    Historical judgements without those fields remain readable and cannot create
    an empty revision candidate.
    """

    statement = judgement.get("proposed_statement")
    confidence = judgement.get("proposed_confidence")
    statement = statement.strip() if isinstance(statement, str) and statement.strip() else None
    confidence = confidence if confidence in ("low", "medium", "high") else None
    return statement, confidence


#: The statuses an effect reports when the forecast chain really moved.
_FORECAST_MOVED = frozenset({"fresh", "published", "duplicate"})


def forecast_moved(effect: Mapping[str, Any] | None) -> bool:
    """Whether the judgement's own effect already published a forecast version."""

    return bool(
        effect
        and effect.get("kind") == "revise_forecast"
        and effect.get("status") in _FORECAST_MOVED
        and effect.get("model_version_ref")
    )


def resolve_named_theses(
    connection: sqlite3.Connection, company_ref: str, refs: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split the thesis versions a judgement named into current and superseded.

    Current means what ``company_theses`` shows the judge, so the forward path
    and a later backfill bind to the same thing.  A ref that names no thesis
    version this company can see is dropped: the judge was never allowed to
    cite it.
    """

    named = [ref for ref in dict.fromkeys(refs or ()) if isinstance(ref, str)]
    if not named:
        return [], []
    current = {thesis["ref"]: thesis for thesis in company_theses(connection, company_ref)}
    live = [current[ref] for ref in named if ref in current]
    superseded: list[str] = []
    for ref in named:
        if ref in current:
            continue
        try:
            row = connection.execute(
                "SELECT thesis_id FROM thesis_versions WHERE version_id=?", (ref,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None and any(
            thesis.get("thesis_ref") == row["thesis_id"] for thesis in current.values()
        ):
            superseded.append(ref)
    return live, superseded


def record_judgement_followups(
    judgements: EventJudgementAuthority,
    *,
    judgement_ref: str,
    event_ref: str,
    company_ref: str,
    judgement: Mapping[str, Any],
    effect: Mapping[str, Any] | None,
    mission: Mapping[str, Any],
    actor_ref: str,
    model_version_ref: str | None = None,
    reflection_ref: str | None = None,
) -> dict[str, Any]:
    """Write the governed follow-up a decision other than NO_CHANGE owes.

    The one place the forward path (the judgement child) and the backfill
    sweep both go through.  In order:

    1. every admitted thesis version the judgement named, still current,
       gets an ADR-0007 ThesisRevisionCandidate -- whatever the action was,
       because the decision word is itself a statement about the thesis;
    2. a ``revise_forecast`` whose effect did not already publish a version
       becomes a ForecastRevisionProposal against the company's model, when
       there is one (the mission did not grant the write, the invariants
       refused it, or it predates this code);
    3. a judgement that got neither leaves one labelled notice for the owner.

    Every write is keyed on the judgement, so calling this twice writes once.
    """

    decision = judgement.get("decision")
    if decision == "NO_CHANGE" or decision not in DECISION_VOCABULARY:
        return {"status": "not_owed", "candidates": [], "proposal_ref": None,
                "notice_ref": None}
    autonomy = mission["autonomy"]
    # ADR-0007: the candidate (and the notice that stands in for one) needs
    # the scope and its checkpoint. The forecast proposal never did: it is
    # what a revise_forecast becomes precisely when a write is not granted.
    thesis_granted = ("thesis_revision_candidate" in autonomy["may_write"]
                      and "thesis_revision_candidate" in autonomy["human_checkpoints"])
    action = str(judgement.get("action") or "")
    because = str(judgement.get("because") or "")[:MAX_BECAUSE_CHARS] or decision
    evidence = [ref for ref in (judgement.get("citations") or ()) if isinstance(ref, str)]
    evidence = evidence or [event_ref]
    named = [ref for ref in (judgement.get("thesis_refs") or ()) if isinstance(ref, str)]
    connection = judgements.connection
    live, superseded = resolve_named_theses(connection, company_ref, named)

    # 2026-10-06: a candidate is written only when the judgement proposes
    # something the thesis could become.  One that names current theses and
    # proposes nothing is the judgement keeping them (live legacy ACN,
    # THESIS_WEAKENED, "首选判断是维持…"): accepting such a candidate is a
    # rewrite of an unchanged view and the thesis authority refuses it
    # (ADR-0008), so it becomes a governed no-revision follow-up instead.
    # ``revise_thesis`` is the judge asking a person to rewrite the thesis, so
    # it stays a candidate even without proposed words; every other action
    # with nothing proposed is the judgement keeping the thesis as it is.
    proposed_statement, proposed_confidence = proposed_thesis_revision(judgement)
    proposes = proposed_confidence is not None
    if action == "revise_thesis" and not proposes and live:
        return {"status": "blocked", "reason": "incomplete_revision_proposal", "candidates": [],
                "proposal_ref": None, "notice_ref": None,
                "display_reason": "旧建议未写明新的论点和证据充分度，需基于当前证据重新复核。"}
    candidates = [] if not (thesis_granted and proposes) else [
        judgements.record_thesis_candidate(
            # ``company_theses`` names a version ``ref``; the candidate binds
            # by ``id``. Passing the row through unchanged was a KeyError the
            # revise_thesis path never lived long enough to raise.
            judgement_ref=judgement_ref, thesis={**thesis, "id": thesis["ref"]},
            company_ref=company_ref,
            decision=decision, because=because, evidence_refs=evidence,
            falsifier_ref=next(iter(thesis.get("falsifier_refs") or ()), None),
            proposed_statement=proposed_statement,
            proposed_confidence=proposed_confidence,
            mission=mission, actor_ref=actor_ref, reflection_ref=reflection_ref,
        )["id"]
        for thesis in live
    ]
    no_revision_ref = None
    if thesis_granted and live and not candidates:
        no_revision_ref = judgements.record_no_revision_followup(
            judgement_ref=judgement_ref, company_ref=company_ref, decision=decision,
            action=action, because=because, evidence_refs=evidence,
            maintained_theses=live, mission=mission, actor_ref=actor_ref,
            reflection_ref=reflection_ref,
        )["id"]

    proposal_ref = None
    moved = forecast_moved(effect)
    change = judgement.get("forecast_change")
    if (action == "revise_forecast" and not moved and model_version_ref
            and isinstance(change, Mapping)
            and all(change.get(key) for key in ("driver_ref", "period_end", "value"))):
        reason = str((effect or {}).get("reason") or "").strip() or (
            "the judgement's own effect did not revise the model")
        proposal_ref = judgements.record_forecast_proposal(
            judgement_ref=judgement_ref, company_ref=company_ref,
            model_version_ref=model_version_ref, change=change, decision=decision,
            because=because, evidence_refs=evidence, mission=mission,
            actor_ref=actor_ref, reason=reason,
        )["id"]

    if not thesis_granted and not moved and proposal_ref is None:
        return {"status": "queued", "candidates": [], "proposal_ref": None,
                "forecast_version_ref": None, "notice_ref": None,
                "reason": "the mission does not grant thesis_revision_candidate with its "
                          "checkpoint; ADR-0007's follow-up waits for that mission version"}

    notice_ref = None
    reopen = None
    if superseded and thesis_granted:
        reopen = reopen_superseded_thesis(judgements, company_ref, mission)
    if (not candidates and no_revision_ref is None and not moved
            and proposal_ref is None):
        admitted = [thesis["ref"] for thesis in company_theses(connection, company_ref)]
        if superseded:
            kind = "thesis_superseded"
            reason = ("the thesis version this judgement named was revised by a person "
                      "after it: " + ", ".join(superseded))
        else:
            kind = "needs_thesis"
            reason = (
                "no admitted thesis was named by this judgement"
                + ("" if admitted else " and none is admitted for this company")
                + ("; no company model to propose a forecast change against"
                   if action == "revise_forecast" and not model_version_ref else "")
            )
        notice_ref = judgements.record_followup_notice(
            judgement_ref=judgement_ref, company_ref=company_ref, notice_kind=kind,
            decision=decision, action=action, because=because, reason=reason,
            evidence_refs=evidence, named_thesis_refs=named,
            admitted_thesis_refs=admitted, model_version_ref=model_version_ref,
            mission=mission, actor_ref=actor_ref,
        )["id"]
    return {
        "status": "recorded",
        "candidates": candidates,
        "proposal_ref": proposal_ref,
        "forecast_version_ref": (effect or {}).get("model_version_ref") if moved else None,
        "notice_ref": notice_ref,
        "no_revision_ref": no_revision_ref,
        "reopen": reopen,
    }


def reopen_superseded_thesis(judgements, company_ref, mission):
    """Use the existing C thresholds; a superseded version is not a terminal exemption."""
    from .deliverable_reopen import GateReopenAuthority
    from .redraft_policy import auto_reopen
    path = getattr(judgements.store, "path", ":memory:")
    if path == ":memory:":
        return {"status": "unavailable", "reason": "persistent evidence state is unavailable"}
    return auto_reopen(GateReopenAuthority(judgements.store), judgements.connection,
                       Path(path).parent, company_ref, mission=mission)


def sweep_judgement_followups(
    judgements: EventJudgementAuthority,
    mission: Mapping[str, Any],
    *,
    actor_ref: str,
    model_version_for: Callable[[str], str | None] | None = None,
    apply: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """Backfill: route every owed judgement through ``record_judgement_followups``.

    Reads the ledger, not a batch: every primary judgement other than
    NO_CHANGE that has no follow-up yet.  A grouped alias shares its primary's
    follow-up and is skipped.  Safe on every tick -- a judgement that already
    has a candidate, a proposal, a notice or a published forecast version is
    left alone, and each write is keyed on the judgement.  ``apply=False``
    reports what would be written and writes nothing.
    """

    report: dict[str, Any] = {"mode": "apply" if apply else "dry_run", "examined": 0,
                              "satisfied": 0, "grouped": 0, "owed": [], "written": [],
                              "errors": []}
    rows = judgements.connection.execute(
        "SELECT judgement_id, event_ref, company_ref, record_json FROM event_judgements "
        "WHERE decision != 'NO_CHANGE' ORDER BY created_at, judgement_id"
    ).fetchall()
    for row in rows:
        if len(report["owed"]) >= limit:
            break
        report["examined"] += 1
        try:
            record = json.loads(row["record_json"])
        except (TypeError, json.JSONDecodeError):
            report["errors"].append({"judgement_ref": row["judgement_id"],
                                     "reason": "record_json is not JSON"})
            continue
        effect = record.get("effect") or {}
        if effect.get("kind") == "grouped_judgement":
            report["grouped"] += 1
            continue
        existing = judgements.followups_for(row["judgement_id"])
        if any(existing.values()) or forecast_moved(effect):
            report["satisfied"] += 1
            continue
        model_ref = None
        if record.get("action") == "revise_forecast" and model_version_for is not None:
            try:
                model_ref = model_version_for(row["company_ref"])
            except Exception:  # noqa: BLE001 - no model is "no model", not a crash
                model_ref = None
        live, superseded = resolve_named_theses(
            judgements.connection, row["company_ref"], record.get("thesis_refs") or ())
        entry = {"judgement_ref": row["judgement_id"], "company_ref": row["company_ref"],
                 "decision": record.get("decision"), "action": record.get("action"),
                 "model_version_ref": model_ref,
                 "current_thesis_refs": [thesis["ref"] for thesis in live],
                 "superseded_thesis_refs": superseded}
        report["owed"].append(entry)
        if not apply:
            continue
        reflection = judgements.reflection_for(row["judgement_id"])
        try:
            written = record_judgement_followups(
                judgements, judgement_ref=row["judgement_id"], event_ref=row["event_ref"],
                company_ref=row["company_ref"], judgement=record, effect=effect,
                mission=mission, actor_ref=actor_ref, model_version_ref=model_ref,
                reflection_ref=None if reflection is None else reflection["id"],
            )
        except EventJudgementError as exc:
            report["errors"].append({"judgement_ref": row["judgement_id"],
                                     "reason": f"{type(exc).__name__}: {exc}"})
            continue
        report["written"].append({"judgement_ref": row["judgement_id"], **written})
    return report


# ---------------------------------------------------------------------------
# the budget pool
# ---------------------------------------------------------------------------


def pool(mission: Mapping[str, Any]) -> dict[str, Any]:
    from .budget_pools import pool_caps

    cap_micros = pool_caps(mission["budget"])["caps_micros"][POOL_NAME]
    day_micros = int(Decimal(str(mission["budget"]["max_daily_cost_usd"])) * 1_000_000)
    return {
        "pool": POOL_NAME,
        "share": str(Decimal(cap_micros) / Decimal(day_micros)),
        "cap_usd": str((Decimal(cap_micros) / 1_000_000).quantize(Decimal("0.000001"))),
        "cap_micros": cap_micros,
    }


def pool_state(
    authority: EventJudgementAuthority, mission: Mapping[str, Any], *, day: str,
    budget_db: str | Path | None = None, scheduler_db: str | Path | None = None,
) -> dict[str, Any]:
    """The day's account: what the day ledger says this pool's purposes cost.

    The lane's own ``event_response_spend`` book is written from what a call
    *returned*, so it cannot see a call it never heard back from.  Live
    (2026-09-23/24) 135 Opus judgements came back to the lane as "this request
    is already running" or a transport failure -- no provenance, so nothing
    booked -- and were then settled in the day ledger at 0.27-0.28 USD each:
    the book said 38.2 USD for 09-24 while the ledger said 55.3, and the 50 USD
    cap was checked against the smaller number.  The book also keeps the cost
    a call reported on return, not the settlement the ledger later made.

    So when the lane knows where the day ledger is, the pool is the ledger:
    admissions settled at what they cost (corrections included), open
    reservations at what they hold, for every purpose that spends this pool --
    read with the same function the claim-support ceiling uses.  A ledger that
    cannot be read leaves no room (``ledger_error``): a ceiling nobody can read
    is not a ceiling that has room.  With no ledger configured (tests, a bare
    Core) the book is still the account.

    ``scheduler_db`` (default: ``scheduler.sqlite`` beside the ledger) is where
    ``purpose_spend_micros`` looks up why a call failed, so that a call the
    adapter refused over contract wiring before sending it -- settled at its
    full reservation before 4fa1e3c4 -- does not count against the pool.
    """

    state = pool(mission)
    if budget_db is None:
        spent = authority.day_cost_micros(day)
        return {**state, "day": day, "spent_micros": spent, "source": "lane_book",
                "remaining_micros": max(0, state["cap_micros"] - spent)}
    from .claim_support_verification import ClaimSupportError, purpose_spend_micros

    try:
        by_purpose = {purpose: purpose_spend_micros(budget_db, purpose, day,
                                                    scheduler_db=scheduler_db)
                      for purpose in POOL_LEDGER_PURPOSES}
    except ClaimSupportError as exc:
        return {**state, "day": day, "spent_micros": None, "source": "day_ledger",
                "remaining_micros": 0, "ledger_error": str(exc)}
    spent = sum(by_purpose.values())
    return {**state, "day": day, "spent_micros": spent, "source": "day_ledger",
            "spent_by_purpose": by_purpose,
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
            "statement": str(wire.get("statement") or ""),
            "mechanism": str(wire.get("mechanism") or "")[:800],
            "confidence": wire.get("confidence"),
            "falsifier_refs": list(wire.get("falsifier_refs") or ()),
            "content_hash": wire.get("content_hash"),
            "question_ref": wire.get("question_ref"),
            "stance": wire.get("stance"),
            "proposition_direction": wire.get("proposition_direction"),
            "view_status": wire.get("view_status"),
            "subject_ref": subject,
            "created_at": row["created_at"],
        })
    return theses


MAX_ASSUMPTIONS_PER_DRIVER = 3


def model_drivers(model_version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The driver rows and the nearest assumptions, small enough for a prompt."""

    if not model_version:
        return []
    # Imported here rather than at module scope, as ``revise_assumptions``
    # already is: the forecast layer reaches back into this one, and the two
    # modules only meet when a model version is actually in hand.
    from .model_forecast_driver import proxy_gaps

    by_driver: dict[str, list[dict[str, Any]]] = {}
    for assumption in model_version.get("assumptions") or ():
        if assumption.get("superseded_by"):
            continue
        by_driver.setdefault(str(assumption["driver_ref"]), []).append({
            "period_end": str(assumption["period"]["end"]),
            "measure": str(assumption["measure"]),
            "value": str(assumption["value"]),
            "kind": str(assumption["kind"]),
            # W4: a market proxy travels with the distance between it and what
            # the company realises, or it does not travel. The forecast layer
            # refuses an assumption that cites one without saying; carrying the
            # sentence here is what makes the refusal worth having, because the
            # judge is who would otherwise read a spread as a realised margin.
            "proxy_gaps": proxy_gaps(assumption),
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

    from .claim_retirement import retired_claim_version_refs

    # Retired less reinstated (2026-09-24).
    retired = retired_claim_version_refs(connection)
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


#: W4: which event kinds carry a derived context block, and who builds it.
#: A kind that is not here gets no block, which is the correct behaviour for
#: the eleven kinds whose payload already says everything there is to say.
DERIVED_CONTEXT_KINDS: frozenset[str] = frozenset({
    "insider_transaction", "insider_trading_plan", "buyback_disclosure",
    # ADR-0015: the release's preliminary rows and the provisional
    # reconciliation, read from the ledger.
    "earnings_release",
})


def derived_context(
    event: Mapping[str, Any],
    *,
    connection: sqlite3.Connection,
    recent_events: Sequence[Mapping[str, Any]] = (),
    price: Mapping[str, Any] | None = None,
    market_cap: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The computed block for one event, or ``None`` when the kind has none.

    Deterministic and free: every figure is arithmetic over ledgers this Core
    already holds, so building it costs a handful of queries and no model call,
    and the same ledger produces the same block a month later when somebody
    re-reads the judgement and wants to check it.

    Failure is a missing block and a reason, never a raised exception: a
    judgement that cannot be made because a derived comparison could not be
    computed would be the arithmetic deciding what gets judged.
    """

    kind = event.get("kind")
    if kind not in DERIVED_CONTEXT_KINDS:
        return None
    company_ref = str(event.get("company_ref"))
    try:
        if kind == "earnings_release":
            from .earnings_release_intake import judgement_lines

            return {"context": dict(event.get("payload") or {}),
                    "lines": judgement_lines(event, connection),
                    "refs": [event["id"], *event.get("source_refs", ())]}
        if kind == "insider_trading_plan":
            payload = event.get("payload") or {}
            return {
                "context": payload,
                "lines": [
                    "SEC Item 5 / Item 9B trading-arrangement disclosure (a plan action, not an executed trade):",
                    f"person={payload.get('person_name')}; title={payload.get('person_title')}; "
                    f"action={payload.get('action')}; plan_type={payload.get('plan_type')}; "
                    f"action_date={payload.get('action_date')}; direction={payload.get('securities_direction')}; "
                    f"aggregate_shares={payload.get('aggregate_shares')}; expiration_date={payload.get('expiration_date')}",
                    f"filed in {payload.get('form')} {payload.get('accession')} on "
                    f"{payload.get('filing_date')}; filed text: {payload.get('material_terms_excerpt')}",
                ],
                "refs": [event["id"], *event.get("source_refs", ())],
            }
        if kind == "insider_transaction":
            from .insider_context import build_insider_context
            from .insider_context import prompt_block as insider_block

            built = build_insider_context(
                event, company_events=recent_events, connection=connection
            )
            return {"context": built, "lines": insider_block(built),
                    "refs": built["refs"]}
        if (event.get("payload") or {}).get("market") == "HK":
            from .hkex_filings_adapter import buyback_context as hk_buyback_context

            def hk_row(item):
                payload = item.get("payload") or {}
                return {
                    "trading_date": payload.get("period_end"),
                    "shares_repurchased": payload.get("shares_purchased"),
                    "aggregate_price_paid": payload.get("total_paid"),
                    "currency": payload.get("currency"),
                    "mandate_to_date_shares": payload.get("cumulative_shares"),
                    "mandate_to_date_pct_of_issued": payload.get("pct_of_issued"),
                }

            history = [item for item in recent_events
                       if item.get("company_ref") == company_ref
                       and item.get("kind") == "buyback_disclosure"
                       and (item.get("payload") or {}).get("market") == "HK"]
            comparable = price if price and price.get("currency") == (event.get("payload") or {}).get("currency") else None
            built = hk_buyback_context(
                hk_row(event), prior_rows=[hk_row(item) for item in history],
                current_price=(comparable or {}).get("close"),
                current_price_ref=(comparable or {}).get("version_ref"),
            )
            refs = [item.get("id") for item in [event, *history]]
            refs += [(event.get("payload") or {}).get("invocation_ref"),
                     (comparable or {}).get("version_ref")]
            return {"context": built,
                    "lines": ["HK daily buyback context (filed total / shares; cumulative since mandate):",
                              json.dumps(built, ensure_ascii=False, sort_keys=True)],
                    "refs": list(dict.fromkeys(ref for ref in refs if ref))}
        from .buyback_context import build_buyback_context, statement_figures
        from .buyback_context import prompt_block as buyback_block
        from .buyback_disclosure import transcript_mentions_buyback

        built = build_buyback_context(
            event,
            company_events=recent_events,
            price=price,
            market_cap=market_cap,
            statements=statement_figures(connection, company_ref),
            transcript_claims=transcript_mentions_buyback(
                connection, company_ref=company_ref,
                document_ref=(event.get("payload") or {}).get("document_ref"),
            ),
        )
        return {"context": built, "lines": buyback_block(built), "refs": built["refs"]}
    except Exception as exc:  # noqa: BLE001 - a block that cannot be built is a
        # missing block, not a refusal to judge the event.
        return {
            "context": None,
            "lines": [
                "",
                f"## Derived context for this {kind} could not be built: "
                f"{type(exc).__name__}: {exc}",
                "Judge the event on its payload alone and say that you did.",
            ],
            "refs": [],
        }


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
    prior_view: Sequence[Mapping[str, Any]] = (),
    price: Mapping[str, Any] | None = None,
    market_cap: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the one bounded call is allowed to see, and nothing else."""

    company_ref = event["company_ref"]
    derived = derived_context(
        event, connection=connection, recent_events=recent_events,
        price=price, market_cap=market_cap,
    )
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
        # W3: our own earlier file on this company, if there is one. Supplied
        # by the caller rather than read here, so this module keeps its one
        # dependency direction and a Core with no prior corpus simply passes
        # nothing.
        "prior_view": list(prior_view or ()),
        "source_keys": list(source_keys),
        "derived": None if derived is None else derived["context"],
        "derived_lines": [] if derived is None else list(derived["lines"]),
        "derived_refs": [] if derived is None else list(derived["refs"]),
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
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The short note a ``note`` decision produces.

    Published through the deliverable authority the mission already grants, so
    the note inherits the rule that matters: a figure with no live Claim
    behind it is refused, and the drafter is expected to write the gap marker
    rather than a number it cannot source.
    """

    title = f"{event['kind']} {event['occurred_at'][:10]}"
    claim_refs = [ref for ref in judgement["citations"] if ref.startswith("claim-version:")]
    sections: list[dict[str, Any]] = [{
        "title": title, "body": judgement["note"], "claim_refs": claim_refs, "numbers": [], "gaps": [],
    }]
    extra: dict[str, Any] = {}
    material = (context or {}).get("result") or {}
    if material.get("tags"):
        # Batch F: the note's figures and the 实际 vs 预期 lines are printed
        # with the passage or pack number each came from.
        allow = publisher_resolves_pack_numbers(deliverables)
        bindings, texts = material.get("tags") or {}, material.get("tag_texts") or {}
        passages = {ref: texts.get(tag, "") for tag, ref in bindings.items() if str(ref).startswith("doc-passage:")}
        numbers_by_ref = ({ref: texts.get(tag, "") for tag, ref in bindings.items()
                           if str(ref).startswith("pack-json:")} if allow else {})
        numbers_by_ref.update(payload_number_sources(event) if allow else {})
        body, entries, _dropped = _sourced_prose(judgement["note"], sources=numbers_by_ref, passages=passages)
        sections[0].update({"body": body, "numbers": [
            {k: v for k, v in entry.items() if k != "source_text"} for entry in entries][:60]})
        if judgement.get("result"):
            row = {**judgement, "result_material": {**material, "dates": material.get("dates"),
                                                    "main_event_ref": material.get("main_event_ref")}}
            result_body, result_entries = _result_section_body(row, allow_pack_numbers=allow)
            sections.append({"title": "实际 vs 预期", "body": result_body, "claim_refs": [],
                             "numbers": [{k: v for k, v in entry.items() if k != "source_text"}
                                         for entry in result_entries][:60], "gaps": []})
            entries = entries + result_entries
        if allow:
            extra["pack_numbers"] = {entry["number_ref"]: entry.get("source_text") or ""
                                     for entry in entries if entry.get("number_ref")}
    return deliverables.publish(
        kind="event_note",
        subject_ref=event["company_ref"],
        mission=mission,
        playbook=playbook,
        template_ref="template:event-note:p14a:v1",
        sections=sections,
        summary=f"{event['id']} -> {judgement['decision']} / {judgement['action']}: "
                f"{judgement['because'][:400]}",
        gaps=[],
        model_invocation_refs=[
            ref for ref in [(judgement.get("model") or {}).get("invocation_ref")] if ref
        ],
        actor_ref=actor_ref,
        idempotency_key=f"event-note:{event['id']}",
        **extra,
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
    refs = [{"kind": "event", "ref": event["id"], "concept": None,
             "period_end": None, "accession": None}]
    for ref in event.get("source_refs") or []:
        kind = ("doc_passage" if str(ref).startswith("doc-passage:") else
                "figure" if str(ref).startswith(("earnings-release-outlook:", "consensus-estimate-version:", "guidance:", "consensus:")) else None)
        if kind:
            refs.append({"kind": kind, "ref": ref, "concept": None, "period_end": None, "accession": None})
    return refs


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
    material_opener: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Do what the decision said; a NO_CHANGE note waits for the daily digest.

    Batch F, decision-action consistency: ``action=research`` always opens a
    MaterialRequest through ``material_opener`` (batch E's
    ``open_for_effect``), so a research decision has a consumer.
    """

    if judgement["decision"] == "NO_CHANGE" and judgement["action"] == "note":
        effect: dict[str, Any] = {
            "kind": "note", "status": "deferred",
            "reason": "NO_CHANGE: rolled into the company's daily event_note digest "
                      "instead of a version of its own",
        }
    else:
        effect = _apply_effect(
            event=event, judgement=judgement, context=context, mission=mission,
            playbook=playbook, deliverables=deliverables,
            forecast_models=forecast_models, model_version=model_version,
            research_admitter=research_admitter, actor_ref=actor_ref,
        )
    if judgement["action"] == "research":
        effect = {**effect, **open_research_material(event, judgement, context, material_opener)}
    if judgement["decision"] == "NO_CHANGE":
        effect = {**effect, "daily_digest": DAILY_DIGEST_MARKER}
    return effect


def open_research_material(
    event: Mapping[str, Any], judgement: Mapping[str, Any], context: Mapping[str, Any],
    material_opener: Callable[[Mapping[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    """The MaterialRequest a research decision (or 证据不足) opens, as effect fields."""

    from .event_judgement_result import gap_need, research_need

    result = judgement.get("result") or {}
    material = context.get("result") or {}
    if result.get("evidence_status") == "insufficient" and not material.get("body_read"):
        spec = gap_need(event, material)
    else:
        spec = research_need(event, judgement)
    if material_opener is None:
        return {"material_request": {"id": None, "status": "not_opened",
                                     "reason": "no material request ledger on this run",
                                     "need": spec["need"], "period": spec["period"]}}
    try:
        opened = material_opener(spec)
    except Exception as exc:  # noqa: BLE001 - a ledger refusal is an outcome, not a crash
        return {"material_request": {"id": None, "status": "refused",
                                     "reason": f"{type(exc).__name__}: {exc}"[:300]}}
    return {"material_request": {"id": opened.get("id"), "status": opened.get("status") or "open",
                                 "write_status": opened.get("write_status"), "via": opened.get("via"),
                                 "kind": spec["kind"], "period": spec["period"]}}


def _apply_effect(
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
                playbook=playbook, actor_ref=actor_ref, context=context,
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
        from .economic_invariants import UNAVAILABLE, EconomicInvariantRefused
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
                    **({"outside_band": {"reason": change["outside_band_reason"]}}
                       if change.get("outside_band_reason") else {}),
                }],
                change_reason="driver_event",
                evidence_refs=_event_evidence_refs(event),
                actor_ref=actor_ref,
                decision=judgement["decision"],
                mission_version_ref=mission["id"],
            )
            published = forecast_models.publish(body)
        except EconomicInvariantRefused as exc:
            # P17b: a judgement may move an assumption; it may not move one
            # into a chain that could not describe a company. The refusal is
            # already on the record with every reason.
            return {"kind": "revise_forecast", "status": UNAVAILABLE,
                    "reason": "; ".join(exc.report.reasons)}
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


_DIGEST_SENTENCE_RE = re.compile(r"[^。！？；;!?\n]+(?:[。！？；;!?]|\n|$)")


def payload_number_sources(event: Mapping[str, Any] | None) -> dict[str, str]:
    """``event-payload:<event>/<field>`` -> ``"<field> <value>"`` for every numeric payload field.

    A price move's close, previous close and returns are ledger facts the
    event carries; a digest sentence restating them cites them here (batch F:
    rendered with their source instead of elided).
    """

    out: dict[str, str] = {}
    if not event:
        return out
    for field, value in sorted((event.get("payload") or {}).items()):
        if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            continue
        text = str(value)
        if re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?", text):
            out[f"event-payload:{event['id']}/{field}"] = f"{field} {text}"
    return out


def _sourced_entries(sentence: str, sources: Mapping[str, str],
                     passages: Mapping[str, str]) -> list[dict[str, Any]] | None:
    """Number entries for every figure in ``sentence``, or None when one has no source."""

    from .event_judgement_result import _bare, _source_values
    from .mission_deliverable import value_tokens

    entries: list[dict[str, Any]] = []
    for token in value_tokens(sentence):
        bare = _bare(token)
        found = None
        for ref, text in passages.items():
            if bare is not None and bare in _source_values(text):
                found = {"text": token, "passage_ref": ref}
                break
        if found is None:
            # The closest source first: the exact digits as written, then (for a
            # percent) a percent field, then any field holding the value.
            ranked = sorted(sources.items(), key=lambda item: (
                token.lstrip("$€£+").rstrip("%") not in item[1],
                token.endswith("%") and "percent" not in item[0], item[0]))
            for ref, text in ranked:
                if bare is not None and bare in _source_values(text):
                    found = {"text": token, "number_ref": ref, "source_text": text[:400]}
                    break
        if found is None:
            return None
        entries.append(found)
    return entries


def _sourced_prose(text: Any, *, sources: Mapping[str, str], passages: Mapping[str, str] | None = None,
                   claim_numbers: Sequence[Mapping[str, Any]] = (), limit: int = 1200,
                   ) -> tuple[str, list[dict[str, Any]], int]:
    """The judgement's own words with their figures sourced; unsourceable sentences left out.

    Replaces the old elision (〔…〕 in place of a figure): a figure is either
    bound to the passage, ledger field or Claim it restates and printed, or
    its sentence is not printed.  Returns (body, number entries, dropped).
    """

    from .mission_deliverable import unsourced_numbers

    cleaned = " ".join(str(text or "").split())[:limit]
    kept: list[str] = []
    entries: list[dict[str, Any]] = []
    dropped = 0
    for sentence in (match.group(0).strip() for match in _DIGEST_SENTENCE_RE.finditer(cleaned)):
        if not sentence:
            continue
        if not unsourced_numbers(sentence, list(claim_numbers)):
            kept.append(sentence)
            continue
        found = _sourced_entries(sentence, sources, passages or {})
        if found is None:
            dropped += 1
            continue
        kept.append(sentence)
        entries.extend(found)
    body = "".join(kept)
    if dropped:
        note = DAILY_DIGEST_DROPPED_NOTE.format(n=dropped) if kept else DAILY_DIGEST_EMPTY_NOTE
        body = (body + "\n" + note) if body else note
    return body, entries, dropped


def _result_section_body(row: Mapping[str, Any], *, allow_pack_numbers: bool) -> tuple[str, list[dict[str, Any]]]:
    """A result-contract judgement's lines, each figure bound; unbindable parts left out."""

    from .event_judgement_result import summary_lines
    from .mission_deliverable import unsourced_numbers

    lines, entries = summary_lines(row)
    usable = [entry for entry in entries if allow_pack_numbers or "passage_ref" in entry]
    kept, dropped = [], 0
    for line in lines:
        if unsourced_numbers(line, [{"text": entry["text"]} for entry in usable]):
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        kept.append(DAILY_DIGEST_DROPPED_NOTE.format(n=dropped))
    return "\n".join(kept), usable


def build_daily_digest(
    company_ref: str,
    day: str,
    judgements: Sequence[Mapping[str, Any]],
    *,
    live_claim_refs: set[str] | None = None,
    events: Mapping[str, Mapping[str, Any]] | None = None,
    claim_texts: Mapping[str, str] | None = None,
    allow_pack_numbers: bool = False,
) -> dict[str, Any] | None:
    """One company's closed UTC day of judgements as event_note sections.

    Pure and ordered by ``(created_at, id)``, so the same ledger always renders
    the same document.  ``None`` when the day holds no NO_CHANGE judgement
    carrying the digest marker: everything else was already published.

    Batch F: figures are rendered with their sources -- a result-type
    judgement's 实际 vs 预期 lines bind each figure to its passage or pack
    number, other judgements bind restated figures to the event's own payload
    (``events``) or to the Claims they cite (``claim_texts``).  A sentence with a
    figure nothing sources is left out and counted, never elided in place.
    ``allow_pack_numbers`` says the publisher resolves ``number_ref`` entries.
    """

    rows = sorted(
        (row for row in judgements
         if row.get("company_ref") == company_ref
         and str(row.get("created_at") or "")[:10] == day),
        key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")),
    )
    deferred = [
        row for row in rows
        if row.get("decision") == "NO_CHANGE"
        and (row.get("effect") or {}).get("daily_digest") == DAILY_DIGEST_MARKER
    ]
    if not deferred:
        return None
    no_change = sum(1 for row in rows if row.get("decision") == "NO_CHANGE")
    if no_change == len(rows):
        opening = ("当日考察的输入全部研判为 NO_CHANGE，未改变现行判断；"
                   "下面逐条列出输入与结论，不另发单条事件笔记。")
    else:
        opening = ("当日研判为 NO_CHANGE 的输入未改变现行判断，逐条列出输入与结论；"
                   "其余研判已即时发布为独立事件笔记，一并列出以便对照。")
    sections: list[dict[str, Any]] = [{
        "title": f"{day} 事件研判日汇总（UTC）· 考察输入 {len(rows)} 条 · "
                 f"NO_CHANGE {no_change} 条",
        "body": opening, "claim_refs": [], "numbers": [], "gaps": [],
    }]
    pack_numbers: dict[str, str] = {}
    for row in rows[:DAILY_DIGEST_MAX_ENTRIES]:
        effect = row.get("effect") or {}
        primary = str(effect.get("primary_event_ref") or "") if effect.get("kind") in (
            "grouped_judgement", "repeated_result_event") else ""
        claims = [
            ref for ref in dict.fromkeys(row.get("citations") or ())
            if isinstance(ref, str) and ref.startswith("claim-version:")
            and (live_claim_refs is None or ref in live_claim_refs)
        ][:200]
        numbers: list[dict[str, Any]] = []
        if effect.get("kind") == "grouped_judgement":
            # 2026-10-09 (batch am, coordinator): a grouped input names the
            # event it was judged with -- the ID is in this section's title
            # (the body is held to the no-unsourced-figure rule, an ID is not).
            body = ("与同组主事件一并研判，结论与主事件相同；主事件编号见本节标题，"
                    "其研判正文见本汇总中以该编号为标题的一节。" if primary else
                    "与同组主事件一并研判，结论与主事件相同。")
        elif effect.get("deliverable_version_ref"):
            body = "结论已即时发布为独立事件笔记。"
        elif isinstance(row.get("result"), Mapping):
            body, numbers = _result_section_body(row, allow_pack_numbers=allow_pack_numbers)
        else:
            event_ref = str(row.get("event_ref") or "").split("#", 1)[0]
            sources = payload_number_sources((events or {}).get(event_ref)) if allow_pack_numbers else {}
            from .mission_deliverable import value_tokens

            claim_numbers = [{"text": (claim_texts or {})[ref], "claim_version_ref": ref}
                             for ref in claims if (claim_texts or {}).get(ref)
                             and len((claim_texts or {})[ref]) <= 1000
                             and value_tokens((claim_texts or {})[ref])][:20]
            body, numbers, _dropped = _sourced_prose(
                row.get("note") or row.get("because"), sources=sources, claim_numbers=claim_numbers)
            used_claims = [entry for entry in claim_numbers
                           if any(token in body for token in value_tokens(entry["text"]))]
            numbers = used_claims + numbers
            question = row.get("research_question")
            if question:
                asked, asked_numbers, _ = _sourced_prose(question, sources=sources,
                                                         claim_numbers=claim_numbers, limit=400)
                if asked:
                    body += "\n待研究：" + asked
                    numbers.extend(asked_numbers)
        for entry in numbers:
            if entry.get("number_ref"):
                pack_numbers[entry["number_ref"]] = entry.get("source_text") or ""
        suffix = f" · 主事件 {primary}"[:120] if primary else ""
        sections.append({
            "title": (f"{row.get('event_kind')} · {row.get('decision')}/"
                      f"{row.get('action')} · {row.get('event_ref')}")[:200 - len(suffix)] + suffix,
            "body": body, "claim_refs": claims,
            "numbers": [{key: value for key, value in entry.items() if key != "source_text"}
                        for entry in numbers][:60],
            "gaps": [],
        })
    if len(rows) > DAILY_DIGEST_MAX_ENTRIES:
        sections.append({
            "title": f"另有 {len(rows) - DAILY_DIGEST_MAX_ENTRIES} 条输入未逐条展开",
            "body": "其研判记录照常留存于事件研判账本，可按公司与日期查询。",
            "claim_refs": [], "numbers": [], "gaps": [],
        })
    summary = (
        f"daily digest {company_ref} {day}: {len(rows)} inputs, "
        f"{no_change} NO_CHANGE; judgements: "
        + ",".join(str(row.get("id")) for row in rows)
    )
    if len(summary) > 2000:
        summary = summary[:1997] + "..."
    invocations = [
        ref for ref in dict.fromkeys(
            (row.get("model") or {}).get("invocation_ref") for row in rows
        ) if ref
    ][:40]
    return {
        "company_ref": company_ref, "day": day,
        "judgement_refs": [row.get("id") for row in rows],
        "sections": sections, "summary": summary,
        "model_invocation_refs": invocations,
        "idempotency_key": f"event-note-daily-digest:{company_ref}:{day}",
        "pack_numbers": pack_numbers,
    }


def publisher_resolves_pack_numbers(deliverables: Any) -> bool:
    """Whether ``deliverables.publish`` takes ``pack_numbers`` (batch D's number source)."""

    import inspect

    try:
        return "pack_numbers" in inspect.signature(deliverables.publish).parameters
    except (TypeError, ValueError):
        return False


def publish_daily_digests(
    deliverables: Any,
    *,
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    actor_ref: str,
    today: date,
    lookback_days: int = DAILY_DIGEST_LOOKBACK_DAYS,
) -> list[dict[str, Any]]:
    """Publish the digest for every closed UTC day that still owes one.

    ``today`` is a ``date``; only days strictly before it are closed, so a day
    is digested once, after its last judgement.  The idempotency key is
    checked before anything is rendered, so a tick with nothing owed is one
    query per owed pair and no write; a replay after a crash returns the
    existing version through the authority's own idempotency.
    """

    connection = deliverables.connection
    start = (today - timedelta(days=max(1, int(lookback_days)))).isoformat()
    end = today.isoformat()
    owed = connection.execute(
        "SELECT DISTINCT company_ref, substr(created_at, 1, 10) AS day "
        "FROM event_judgements WHERE created_at >= ? AND created_at < ? "
        "AND decision='NO_CHANGE' "
        "AND json_extract(record_json, '$.effect.daily_digest') = ? "
        "ORDER BY day, company_ref",
        (start, end, DAILY_DIGEST_MARKER),
    ).fetchall()
    results: list[dict[str, Any]] = []
    live: set[str] | None = None
    for pair in owed:
        company_ref, day = pair["company_ref"], pair["day"]
        key = f"event-note-daily-digest:{company_ref}:{day}"
        deliverable_ref = f"mission-deliverable:event_note:{company_ref.rsplit(':', 1)[-1]}"
        if connection.execute(
            "SELECT 1 FROM mission_deliverable_versions WHERE deliverable_ref=? "
            "AND json_extract(record_json, '$.idempotency_key')=?",
            (deliverable_ref, key),
        ).fetchone() is not None:
            continue
        next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        rows = [
            json.loads(row["record_json"]) for row in connection.execute(
                "SELECT record_json FROM event_judgements WHERE company_ref=? "
                "AND created_at >= ? AND created_at < ? "
                "ORDER BY created_at, judgement_id",
                (company_ref, day, next_day),
            ).fetchall()
        ]
        if live is None:
            live = deliverables.live_claim_version_refs()
        allow = publisher_resolves_pack_numbers(deliverables)
        digest = build_daily_digest(
            company_ref, day, rows, live_claim_refs=live, allow_pack_numbers=allow,
            events=_digest_events(connection, rows), claim_texts=_digest_claims(connection, rows))
        if digest is None:
            continue
        try:
            extra = {"pack_numbers": digest["pack_numbers"]} if allow else {}
            published = deliverables.publish(
                kind="event_note", subject_ref=company_ref, mission=mission,
                playbook=playbook, template_ref=DAILY_DIGEST_TEMPLATE_REF,
                sections=digest["sections"], summary=digest["summary"], gaps=[],
                model_invocation_refs=digest["model_invocation_refs"],
                actor_ref=actor_ref, idempotency_key=digest["idempotency_key"], **extra,
            )
        except Exception as exc:  # noqa: BLE001 - one refused company is not the tick's
            results.append({"company_ref": company_ref, "day": day, "status": "refused",
                            "reason": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        results.append({
            "company_ref": company_ref, "day": day,
            "status": published.get("status", "fresh"),
            "deliverable_ref": published.get("deliverable_ref"),
            "deliverable_version_ref": published.get("id"),
            "judgements": len(digest["judgement_refs"]),
        })
    return results


def _digest_events(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in rows:
        ref = str(row.get("event_ref") or "").split("#", 1)[0]
        if not ref or ref in out:
            continue
        try:
            found = connection.execute("SELECT record_json FROM research_events WHERE event_id=?",
                                       (ref,)).fetchone()
        except sqlite3.Error:
            found = None
        if found is not None:
            out[ref] = json.loads(found["record_json"])
    return out


def _digest_claims(connection: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Cited Claims' statements: a figure a NO_CHANGE restates from a Claim is sourced by it."""

    refs = sorted({ref for row in rows for ref in row.get("citations") or ()
                   if isinstance(ref, str) and ref.startswith("claim-version:")})
    out: dict[str, str] = {}
    for ref in refs[:400]:
        try:
            found = connection.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
                                       (ref,)).fetchone()
        except sqlite3.Error:
            found = None
        if found is None:
            continue
        claim = json.loads(found[0])
        statement = str(claim.get("normalized_statement") or claim.get("statement") or "")
        if statement:
            out[ref] = statement[:2000]
    return out


__all__ = [
    "ACTION_VOCABULARY",
    "DAILY_DIGEST_DROPPED_NOTE",
    "DAILY_DIGEST_MARKER",
    "DAILY_DIGEST_TEMPLATE_REF",
    "build_daily_digest",
    "publish_daily_digests",
    "DECISION_ACTIONS",
    "DERIVED_CONTEXT_KINDS",
    "EventJudgementAuthority",
    "EventJudgementConflict",
    "FOLLOWUP_NOTICE_KINDS",
    "FOLLOWUP_NOTICE_LABELS",
    "EventJudgementError",
    "EventJudgementValidationError",
    "MAX_NOTE_SENTENCES",
    "MAX_RECENT_JUDGEMENTS",
    "POOL_LEDGER_PURPOSES",
    "POOL_NAME",
    "POOL_SHARE",
    "PURPOSE",
    "VERIFIER_PURPOSE",
    "REFLECTION_ACTIONS",
    "REFLECTION_PURPOSE",
    "REFLECTION_VERIFIER_PURPOSE",
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
    "derived_context",
    "forecast_moved",
    "judge",
    "market_view_rows",
    "model_drivers",
    "pool",
    "pool_state",
    "publish_event_note",
    "recent_claims",
    "record_judgement_followups",
    "reflect",
    "resolve_named_theses",
    "revision_for_event",
    "reflection_is_owed",
    "route_family_resolver",
    "sweep_judgement_followups",
    "validate_judge_output",
    "validate_reflection_output",
    "validate_verifier_output",
    "verify",
    "verify_reflection",
]
