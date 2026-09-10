"""P15d: the call automation may make, and the decision only a person may.

The blueprint asks for a ``ConvictionCall`` and the owner's self-reflection
rule says what it has to contain: *market 看涨你也看涨、市场看跌你也看跌，没有
价值；要想的是市场定价错在哪，有什么 pathway 让市场向我们的判断靠拢*.  So the
object is not "we like Accenture".  It is four claims held together, and a
record that cannot make all four is refused before a model is asked to write
it:

1. **A variant view.**  Our view *and* the market's view, separately, each
   with references -- consensus, sell-side ratings, the DebateMap's
   ``market_position``, the sales-note and crowd narrative.  When nobody has
   told us what the street thinks, the honest answer is ``available: false``
   with a reason, which is a finding rather than a gap; what is *not* allowed
   is a market view invented to make ours look contrarian.
2. **Where the market is wrong**, as a sentence about a fact, a timing, a
   transmission or a multiple -- not a mood.
3. **A convergence pathway**: the observable events that would drag the price
   towards us, dated from the catalyst calendar wherever a date is known.  A
   call with no pathway is a wish.
4. **Risk and reward against the Playbook's own standards**, checked
   mechanically wherever there are numbers to check.  ``not_met`` does not
   stop the proposal -- the person decides -- but it can never be made to look
   like ``met``.

Two things live here and nowhere else.

**The gate.**  ``precheck`` is entirely deterministic: an active thesis, a
disagreement (a live debate whose ``our_position`` differs from its
``market_position``, or a forecast-versus-consensus gap over the policy
threshold), and material for a variant view.  Agreeing with the market has no
value, so a company that passes none of these is not drafted at all and the
model is never called.  That is the cheapest possible refusal and the reason
the lane's resting state costs nothing.

**The split between proposing and deciding.**  Automation may write a
``ConvictionCallProposal`` and may never write a decision.  A proposal opens
the ``conviction_call`` human checkpoint; ``accept`` / ``reject`` / ``defer``
arrive through a human-governance writer operation, append-only, each bound to
the exact proposal hash it was made about.  ``accepted_calls(window)`` is what
P15c's weekly brief will read when it exists; today it is the reader that
proves an accepted call is retrievable rather than inferred.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .research_playbook import DECISION_VOCABULARY
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

_SCHEMA_PATH = Path(__file__).with_name("conviction_call_schema.sql")

PROPOSAL_TABLE = "conviction_call_proposals"
DECISION_TABLE = "conviction_call_decisions"

WRITE_SCOPE = "conviction_call"
CHECKPOINT_KIND = "conviction_call"

# Long, short, or stand aside.  ``avoid`` is a call: "the market is wrong and
# it is not worth owning either way" is a conclusion an analyst reaches and a
# vocabulary without it would push that answer into silence.
DIRECTIONS: tuple[str, ...] = ("long", "short", "avoid")

# ADR-0001's ordinal confidence, never a float.
CONFIDENCES: tuple[str, ...] = ("low", "medium", "high")

# The horizons the Playbook's own risk/reward standards are written in.  A
# closed list because the standards are keyed on it: "50% upside" means
# nothing until someone says by when, and 做空 must state its horizon.
TIME_HORIZONS: tuple[str, ...] = (
    "3_6_months", "6_12_months", "1_3_years", "3_5_years",
)

# Where a market view may have come from.  Closed, because "the market thinks
# X" sourced from a broker note and sourced from a message board are different
# assertions and the difference has to survive into the record.
MARKET_VIEW_SOURCES: tuple[str, ...] = (
    "consensus",              # a consensus estimate authority
    "sell_side_rating",       # ratings and price targets out of broker notes
    "debate_market_position", # P12c's own reading of where the street stands
    "sales_note",             # S1's sales notes
    "crowd_narrative",        # X / Xueqiu: trend and sentiment, never a fact
    "management_framing",     # what the company itself says the market thinks
)

# Why a company was not drafted.  Each is one mechanical check with one cause.
GATE_REASONS: tuple[str, ...] = (
    "no_active_thesis",
    "no_debate_map_and_no_consensus",
    "we_agree_with_the_market",
    "no_variant_material",
)

DECISIONS: tuple[str, ...] = ("accept", "reject", "defer")
# The two that settle a proposal.  ``defer`` deliberately does not: a call put
# off until after the print is a call still waiting for an answer.
SETTLING_DECISIONS: frozenset[str] = frozenset({"accept", "reject"})

RISK_REWARD_STATUSES: tuple[str, ...] = (
    "met", "not_met", "unavailable", "not_applicable",
)

MAX_STATEMENT_CHARS = 800
MAX_REASON_CHARS = 2000
MAX_SIGNALS = 8
MAX_METRICS = 12
MAX_FALSIFIERS = 8


# ---------------------------------------------------------------------------
# the frozen policy
# ---------------------------------------------------------------------------

# Everything the deterministic layers read is here rather than in an ``if``,
# for the reason every threshold in this repository is: a call that was
# eligible in September has to still be explicable in December.  The same
# bytes are published as ``deploy/phase9/p15d-conviction-policy-v1.json`` and
# a test asserts the two have not drifted.
#
# The risk/reward table is a *reading* of the Playbook's ``risk_reward_standards``
# free text, and it says so: ``playbook_text`` carries the sentence each row
# came from, so a person who thinks the reading is wrong can see what was read.
# Only the rows with a number are checkable; the Playbook's fifth standard
# ("Dalton 只提出研究观点和仓位建议，人类团队决定交易") is not a threshold, it is
# the reason this whole module ends in a human decision.
CONVICTION_POLICY: Mapping[str, Any] = MappingProxyType({
    "policy_ref": "conviction-policy:p15d:v1",
    # How far our forecast has to sit from the street before the gap alone is
    # a reason to write a call.  Ten per cent on a revenue line is a real
    # disagreement; two is a rounding difference between two people's models.
    "min_consensus_gap_percent": "10",
    # A call a week, per company, at most.  A conviction call that arrives
    # every tick is not conviction, it is a feed.
    "max_calls_per_company_per_week": 1,
    # A pathway with no observable signal is a wish; a call with no falsifier
    # is not a research position.
    "min_event_pathway": 1,
    "min_falsifiers": 1,
    "market_view_sources": list(MARKET_VIEW_SOURCES),
    "time_horizons": list(TIME_HORIZONS),
    "risk_reward_standards": [
        {
            "standard_ref": "risk-reward:value:6-12m",
            "directions": ["long"],
            "horizons": ["6_12_months"],
            "rule": "min_upside_percent",
            "threshold": "50",
            "playbook_text": "价值型：6–12 个月 50% upside",
        },
        {
            "standard_ref": "risk-reward:compounder:3-5y",
            "directions": ["long"],
            "horizons": ["3_5_years"],
            # 3x over the holding period is +200%.  The Playbook says 3–5x;
            # the floor is the bottom of its own range.
            "rule": "min_upside_percent",
            "threshold": "200",
            "playbook_text": "长期复利型：目标盈利增速 + 内在价值；长期核心持仓 3–5 年 3–5x",
        },
        {
            "standard_ref": "risk-reward:cycle-catalyst",
            "directions": ["long"],
            "horizons": ["3_6_months", "1_3_years"],
            # "回报必须补偿波动", as the only thing that sentence can mean
            # mechanically: twice as much if we are right as if we are wrong.
            "rule": "min_reward_to_risk",
            "threshold": "2",
            "playbook_text": "周期反转 + 催化型：对了赚多少（盈利 + 估值）/ 错了亏多少；回报必须补偿波动",
        },
        {
            "standard_ref": "risk-reward:short:3-6m",
            "directions": ["short"],
            "horizons": ["3_6_months"],
            "rule": "min_downside_percent",
            "threshold": "30",
            "playbook_text": "做空：3–6 个月 30% downside；交易时间跨度必须写明",
        },
    ],
})

POLICY_REF: str = str(CONVICTION_POLICY["policy_ref"])
POLICY_HASH: str = content_hash(CONVICTION_POLICY)


class ConvictionCallError(RuntimeError):
    """Base error for the conviction-call authority."""


class ConvictionCallValidationError(ConvictionCallError, ValueError):
    """A closed field or argument is invalid."""


class ConvictionCallConflict(ConvictionCallError):
    """Stored bytes disagree with themselves or with the request."""


class ConvictionCallNotFound(ConvictionCallError, LookupError):
    """No such proposal or decision."""


# ---------------------------------------------------------------------------
# small validators
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = MAX_STATEMENT_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConvictionCallValidationError(f"{name} must be non-empty text")
    text = value.strip()
    if len(text) > maximum:
        raise ConvictionCallValidationError(
            f"{name} is longer than {maximum} characters")
    return text


def _optional_text(value: Any, name: str, *, maximum: int = MAX_STATEMENT_CHARS) -> str | None:
    return None if value is None else _text(value, name, maximum=maximum)


def _hash(value: Any, name: str) -> str:
    value = _text(value, name, maximum=128)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ConvictionCallValidationError(f"{name} must be a lowercase SHA-256")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ConvictionCallValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}")
    return str(value)


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ConvictionCallValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]", maximum=512) for item in value]
    if nonempty and not result:
        raise ConvictionCallValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ConvictionCallValidationError(f"{name} must contain unique refs")
    return result


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConvictionCallValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise ConvictionCallValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    return wire


def _human(value: Any, name: str = "actor_ref") -> str:
    actor = _text(value, name, maximum=256)
    if not actor.startswith("human:"):
        raise ConvictionCallValidationError(
            f"{name} must be a human: principal; automation proposes, a person decides")
    return actor


def _decimal(value: Any, name: str) -> Decimal:
    """A percentage, as the string it was written as.

    Carried as text everywhere else so the bytes that were hashed are the
    bytes that were written; parsed only here, where the comparison happens.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ConvictionCallValidationError(f"{name} must be a decimal string")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConvictionCallValidationError(f"{name} is not a number") from exc


def week_key(when: Any) -> str:
    """The ISO year-week a timestamp falls in, e.g. ``2026-W37``.

    The unit the "one call per company per week" rule counts in, and named
    rather than derived at each call site so the lane and the authority cannot
    disagree about where a week ends.
    """

    if isinstance(when, datetime):
        moment = when.date()
    elif isinstance(when, date):
        moment = when
    else:
        text = _text(when, "when", maximum=64)
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                moment = date.fromisoformat(text[:10])
            except ValueError as exc:
                raise ConvictionCallValidationError(
                    "week_key needs a date or an ISO timestamp") from exc
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def call_ref_for(company_ref: str) -> str:
    """One call chain per company, named after it."""

    return "conviction-call:" + _text(company_ref, "company_ref", maximum=256)


def evidence_fingerprint(refs: Iterable[str]) -> str:
    """The exact material a call was drawn from, as one hash.

    Idempotency is keyed on this: a tick that runs again over unchanged
    evidence produces the same fingerprint and therefore the same proposal,
    rather than a second opinion about the same facts.
    """

    return content_hash({"refs": sorted({str(ref) for ref in refs})})


# ---------------------------------------------------------------------------
# the Playbook's risk / reward standards, checked
# ---------------------------------------------------------------------------

def standard_for(
    direction: str, time_horizon: str, policy: Mapping[str, Any] = CONVICTION_POLICY
) -> Mapping[str, Any] | None:
    """The one standard that governs a call of this shape, or ``None``."""

    for row in policy["risk_reward_standards"]:
        if direction in row["directions"] and time_horizon in row["horizons"]:
            return row
    return None


def check_risk_reward(
    *,
    direction: str,
    time_horizon: str,
    upside_percent: Any = None,
    downside_percent: Any = None,
    policy: Mapping[str, Any] = CONVICTION_POLICY,
) -> dict[str, Any]:
    """Whether the Playbook's own standard is met, mechanically.

    Four answers and they are different facts.  ``met`` and ``not_met`` are
    verdicts about numbers that were shown.  ``unavailable`` means a standard
    applies and the call did not carry the numbers to test it against -- which
    is a gap in the call, not a pass.  ``not_applicable`` means the Playbook
    sets no return standard for this shape of call at all: it says nothing
    about standing aside, and a call to avoid a name is not asking for capital.
    """

    direction = _one_of(direction, DIRECTIONS, "direction")
    time_horizon = _one_of(time_horizon, TIME_HORIZONS, "time_horizon")
    base = {
        "standard_ref": None, "rule": None, "required": None, "observed": None,
        "playbook_text": None,
    }
    if direction == "avoid":
        return {**base, "status": "not_applicable",
                "reason": "the Playbook sets no return standard for standing aside"}
    standard = standard_for(direction, time_horizon, policy)
    if standard is None:
        # The only way to get here is a short outside three to six months, and
        # the Playbook is explicit that a short must state that horizon.
        return {
            **base, "status": "not_met",
            "reason": (f"the Playbook sets no {direction} standard at "
                       f"{time_horizon}; 做空的时间跨度必须写明，标准只有 3–6 个月"),
        }
    found = {
        "standard_ref": standard["standard_ref"], "rule": standard["rule"],
        "required": standard["threshold"], "observed": None,
        "playbook_text": standard["playbook_text"],
    }
    threshold = Decimal(standard["threshold"])
    if standard["rule"] == "min_upside_percent":
        if upside_percent is None:
            return {**found, "status": "unavailable",
                    "reason": "the call carries no upside percentage to test"}
        observed = _decimal(upside_percent, "upside_percent")
        found["observed"] = str(upside_percent)
        met = observed >= threshold
        return {**found, "status": "met" if met else "not_met",
                "reason": f"{observed}% upside against a {threshold}% standard"}
    if standard["rule"] == "min_downside_percent":
        if downside_percent is None:
            return {**found, "status": "unavailable",
                    "reason": "the call carries no downside percentage to test"}
        observed = _decimal(downside_percent, "downside_percent")
        found["observed"] = str(downside_percent)
        met = observed >= threshold
        return {**found, "status": "met" if met else "not_met",
                "reason": f"{observed}% downside against a {threshold}% standard"}
    # min_reward_to_risk
    if upside_percent is None or downside_percent is None:
        return {**found, "status": "unavailable",
                "reason": "both the upside and the downside are needed to weigh them"}
    up = _decimal(upside_percent, "upside_percent")
    down = _decimal(downside_percent, "downside_percent")
    if down <= 0:
        # A downside of zero is not an asymmetric trade, it is an unfinished
        # bear case; refusing to divide is refusing to launder it into infinity.
        return {**found, "status": "unavailable",
                "reason": "a downside of zero is an unwritten bear case, not a ratio"}
    ratio = up / down
    found["observed"] = f"{ratio.quantize(Decimal('0.01'))}"
    met = ratio >= threshold
    return {**found, "status": "met" if met else "not_met",
            "reason": f"{found['observed']}x reward to risk against a {threshold}x standard"}


# ---------------------------------------------------------------------------
# the deterministic gate
# ---------------------------------------------------------------------------

def divergent_debates(
    open_debates: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The live debates where we are not standing where the market is.

    Both positions have to be *stated*: a debate whose market position is
    ``available: false`` is one where nobody has told us where the street is,
    and calling that a disagreement would be inventing the other side of an
    argument in order to win it.
    """

    found: list[dict[str, Any]] = []
    for debate in open_debates or ():
        market = debate.get("market_position") or {}
        ours = debate.get("our_position") or {}
        if not market.get("available") or ours.get("state") != "held":
            continue
        if ours.get("side") == market.get("lean"):
            continue
        found.append({
            "debate_ref": str(debate.get("debate_ref") or ""),
            "question": str(debate.get("question") or ""),
            "market_lean": market.get("lean"),
            "our_side": ours.get("side"),
            "refs": sorted(set(list(market.get("refs") or [])
                               + list(ours.get("refs") or []))),
        })
    return found


def wide_consensus_gaps(
    consensus_gap: Mapping[str, Any] | None,
    policy: Mapping[str, Any] = CONVICTION_POLICY,
) -> list[dict[str, Any]]:
    """The metrics where our forecast is far enough from the street to matter."""

    if not consensus_gap or consensus_gap.get("status") != "available":
        return []
    threshold = Decimal(str(policy["min_consensus_gap_percent"]))
    wide: list[dict[str, Any]] = []
    for metric in consensus_gap.get("metrics") or ():
        percent = metric.get("gap_percent")
        if percent is None:
            continue
        try:
            value = Decimal(str(percent))
        except (InvalidOperation, ValueError):
            continue
        if abs(value) >= threshold:
            wide.append(dict(metric))
    return wide


def variant_material(
    *,
    dossier_variant_view: Mapping[str, Any] | None,
    open_debates: Sequence[Mapping[str, Any]],
    consensus_gap: Mapping[str, Any] | None,
) -> list[str]:
    """Which sources can actually tell us what the market thinks.

    Named rather than counted, because "we read the street's view off a broker
    note" and "we read it off a message board" are the two ends of the source
    ladder and a call has to be able to say which one it stood on.
    """

    sources: list[str] = []
    view = dossier_variant_view or {}
    if view.get("status") == "drafted" and view.get("market_view_available"):
        sources.append("dossier_variant_view")
    if any((debate.get("market_position") or {}).get("available")
           for debate in open_debates or ()):
        sources.append("debate_market_position")
    if consensus_gap and consensus_gap.get("status") == "available" \
            and (consensus_gap.get("metrics") or ()):
        sources.append("consensus")
    return sources


def precheck(
    *,
    company_ref: str,
    theses: Sequence[Mapping[str, Any]],
    open_debates: Sequence[Mapping[str, Any]] = (),
    consensus_gap: Mapping[str, Any] | None = None,
    dossier_variant_view: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] = CONVICTION_POLICY,
) -> dict[str, Any]:
    """Is this company worth a call today?  Entirely mechanical, no model.

    Three questions, and a company that fails any of them is not drafted:

    * do we hold a view at all (an active thesis)?
    * is that view *different* from the market's -- a live debate we stand on
      the other side of, or a forecast far enough from the street to matter?
    * can we source what the market thinks, so that the difference is a
      finding rather than an assumption?

    The middle one is the owner's rule in code.  Agreeing with the market in
    more words is not a call, and the cheapest place to refuse it is before
    anything is spent.
    """

    company_ref = _text(company_ref, "company_ref", maximum=256)
    active = [
        {"thesis_version_ref": str(row.get("ref") or row.get("thesis_version_ref") or ""),
         "thesis_ref": row.get("thesis_ref"),
         "statement": row.get("statement"),
         "confidence": row.get("confidence"),
         "falsifier_refs": list(row.get("falsifier_refs") or ())}
        for row in theses or ()
        if (row.get("ref") or row.get("thesis_version_ref"))
    ]
    debates = divergent_debates(open_debates)
    gaps = wide_consensus_gaps(consensus_gap, policy)
    sources = variant_material(
        dossier_variant_view=dossier_variant_view,
        open_debates=open_debates, consensus_gap=consensus_gap,
    )
    checks = {
        "active_thesis": bool(active),
        "divergence": bool(debates or gaps),
        "variant_material": bool(sources),
    }
    reasons: list[str] = []
    if not checks["active_thesis"]:
        reasons.append("no_active_thesis")
    if not checks["divergence"]:
        # Two different silences.  "We have no map and no consensus" is a gap
        # in the plumbing; "we have both and we agree with the street" is a
        # finding about the company, and the weekly review should be able to
        # tell them apart without opening the Core.
        if not (open_debates or (consensus_gap or {}).get("status") == "available"):
            reasons.append("no_debate_map_and_no_consensus")
        else:
            reasons.append("we_agree_with_the_market")
    if not checks["variant_material"]:
        reasons.append("no_variant_material")
    return {
        "company_ref": company_ref,
        "eligible": not reasons,
        "reasons": [_one_of(item, GATE_REASONS, "reasons[]") for item in reasons],
        "checks": checks,
        "theses": active,
        "divergent_debates": debates,
        "consensus_gaps": gaps,
        "variant_sources": sources,
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
    }


# ---------------------------------------------------------------------------
# the closed contract
# ---------------------------------------------------------------------------

_STATEMENT_FIELDS = {"statement", "refs"}
_MARKET_VIEW_FIELDS = {"available", "reason", "statement", "refs", "sources"}
_VARIANT_FIELDS = {
    "our_view", "market_view", "where_market_is_wrong", "convergence_pathway",
}
_GAP_METRIC_FIELDS = {
    "metric", "period", "ours", "consensus", "unit", "gap_percent", "refs",
}
_CONSENSUS_FIELDS = {"status", "reason", "metrics"}
_WINDOW_FIELDS = {"kind", "date", "from", "to"}
_PATHWAY_FIELDS = {"signal", "window", "catalyst_ref", "refs"}
_CASE_FIELDS = {"statement", "percent", "refs"}
_STANDARD_FIELDS = {
    "status", "standard_ref", "rule", "required", "observed", "playbook_text",
    "reason",
}
_RISK_REWARD_FIELDS = {"upside", "downside", "standard"}
_FALSIFIER_FIELDS = {"statement", "falsifier_ref", "thesis_version_ref"}
_ATTRIBUTION_FIELDS = {
    "kind", "work_order_ref", "invocation_ref", "route_decision_ref", "model_family",
}
_RUBRIC_FIELDS = {"rubric_ref", "rubric_hash", "findings"}
_PROPOSAL_FIELDS = frozenset({
    "schema_version", "id", "created_at", "call_ref", "company_ref", "week_key",
    "direction", "decision", "confidence", "time_horizon", "variant_view",
    "consensus_gap", "event_pathway", "risk_reward", "falsifiers",
    "thesis_refs", "debate_refs", "evidence_fingerprint", "precheck", "rubric",
    "drafted_by", "verified_by", "policy_ref", "policy_hash",
    "mission_version_ref", "mission_version_hash", "checkpoint_kind",
    "actor_ref", "content_hash",
})
_DECISION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "proposal_ref", "proposal_hash",
    "decision_number", "decision", "reason", "actor_ref", "content_hash",
})

WINDOW_KINDS: tuple[str, ...] = ("date", "range", "unknown")


def _statement(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_STATEMENT_FIELDS), name)
    return {
        "statement": _text(wire["statement"], f"{name}.statement"),
        "refs": _refs(wire["refs"], f"{name}.refs", nonempty=True),
    }


def _market_view(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_MARKET_VIEW_FIELDS), name)
    if not isinstance(wire["available"], bool):
        raise ConvictionCallValidationError(f"{name}.available must be a boolean")
    if wire["available"]:
        if wire["reason"] is not None:
            raise ConvictionCallValidationError(
                f"{name} has a market view and cannot also say why it has none")
        sources = wire["sources"]
        if not isinstance(sources, list) or not sources:
            raise ConvictionCallValidationError(
                f"{name}.sources must name where the market view came from")
        return {
            "available": True, "reason": None,
            "statement": _text(wire["statement"], f"{name}.statement"),
            "refs": _refs(wire["refs"], f"{name}.refs", nonempty=True),
            "sources": [_one_of(item, MARKET_VIEW_SOURCES, f"{name}.sources[]")
                        for item in sources],
        }
    # "Nobody has told us where the street is" is an answer, and it has to
    # look different from "the street agrees with us".
    if wire["statement"] is not None or wire["refs"] or wire["sources"]:
        raise ConvictionCallValidationError(
            f"{name} is unavailable and cannot also state a view or cite refs")
    return {
        "available": False,
        "reason": _text(wire["reason"], f"{name}.reason"),
        "statement": None, "refs": [], "sources": [],
    }


def validate_variant_view(value: Any, name: str = "variant_view") -> dict[str, Any]:
    """Our view, the market's view, the disagreement, and the way back.

    All four are required.  A call that states only ours is a note; a call
    that states only theirs is a summary; the object exists to hold the
    difference between them.
    """

    wire = _closed(value, set(_VARIANT_FIELDS), name)
    market = _market_view(wire["market_view"], f"{name}.market_view")
    checked = {
        "our_view": _statement(wire["our_view"], f"{name}.our_view"),
        "market_view": market,
        "where_market_is_wrong": _statement(
            wire["where_market_is_wrong"], f"{name}.where_market_is_wrong"),
        "convergence_pathway": _statement(
            wire["convergence_pathway"], f"{name}.convergence_pathway"),
    }
    if not market["available"]:
        # Without a market view there is nothing to be wrong about.  The
        # honest version of this call is one that says the street's view could
        # not be established -- and a call in that state cannot also assert
        # where the street is mistaken, so it is refused rather than softened.
        raise ConvictionCallValidationError(
            f"{name} has no market view; a call that cannot say where the "
            "market stands cannot say where it is wrong")
    return checked


def _gap_metric(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_GAP_METRIC_FIELDS), name)
    return {
        "metric": _text(wire["metric"], f"{name}.metric", maximum=256),
        "period": _text(wire["period"], f"{name}.period", maximum=64),
        "ours": _text(str(wire["ours"]), f"{name}.ours", maximum=64),
        "consensus": _text(str(wire["consensus"]), f"{name}.consensus", maximum=64),
        "unit": _text(wire["unit"], f"{name}.unit", maximum=64),
        "gap_percent": str(_decimal(wire["gap_percent"], f"{name}.gap_percent")),
        "refs": _refs(wire["refs"], f"{name}.refs", nonempty=True),
    }


def validate_consensus_gap(value: Any, name: str = "consensus_gap") -> dict[str, Any]:
    """Our numbers against the street's, or the reason there are none.

    ``unavailable`` with a reason is a first-class answer: on this Core there
    is no consensus authority yet, and a call that quietly omitted the section
    would read as one where we happen to agree.
    """

    wire = _closed(value, set(_CONSENSUS_FIELDS), name)
    status = _one_of(wire["status"], ("available", "unavailable"), f"{name}.status")
    if status == "unavailable":
        if wire["metrics"]:
            raise ConvictionCallValidationError(
                f"{name} is unavailable and cannot also carry metrics")
        return {"status": status,
                "reason": _text(wire["reason"], f"{name}.reason"),
                "metrics": []}
    metrics = wire["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ConvictionCallValidationError(
            f"{name} is available and must carry at least one metric")
    if len(metrics) > MAX_METRICS:
        raise ConvictionCallValidationError(
            f"{name} carries more than {MAX_METRICS} metrics")
    if wire["reason"] is not None:
        raise ConvictionCallValidationError(
            f"{name} is available and cannot also say why it is not")
    return {
        "status": status, "reason": None,
        "metrics": [_gap_metric(row, f"{name}.metrics[{index}]")
                    for index, row in enumerate(metrics)],
    }


def _window(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_WINDOW_FIELDS), name)
    kind = _one_of(wire["kind"], WINDOW_KINDS, f"{name}.kind")
    if kind == "date":
        if wire["from"] is not None or wire["to"] is not None:
            raise ConvictionCallValidationError(f"{name} is one date, not a range")
        return {"kind": kind, "date": _iso_date(wire["date"], f"{name}.date"),
                "from": None, "to": None}
    if kind == "range":
        if wire["date"] is not None:
            raise ConvictionCallValidationError(f"{name} is a range, not one date")
        start = _iso_date(wire["from"], f"{name}.from")
        end = _iso_date(wire["to"], f"{name}.to")
        if end < start:
            raise ConvictionCallValidationError(f"{name} ends before it starts")
        return {"kind": kind, "date": None, "from": start, "to": end}
    if wire["date"] is not None or wire["from"] is not None or wire["to"] is not None:
        raise ConvictionCallValidationError(
            f"{name} has no known date and cannot also carry one")
    return {"kind": kind, "date": None, "from": None, "to": None}


def _iso_date(value: Any, name: str) -> str:
    text = _text(value, name, maximum=32)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ConvictionCallValidationError(f"{name} must be an ISO date") from exc


def _pathway_step(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_PATHWAY_FIELDS), name)
    return {
        "signal": _text(wire["signal"], f"{name}.signal"),
        "window": _window(wire["window"], f"{name}.window"),
        "catalyst_ref": _optional_text(wire["catalyst_ref"], f"{name}.catalyst_ref",
                                       maximum=512),
        "refs": _refs(wire["refs"], f"{name}.refs", nonempty=True),
    }


def validate_event_pathway(value: Any, name: str = "event_pathway") -> list[dict[str, Any]]:
    """The observable things that would drag the market towards us.

    Dated where the catalyst calendar knows a date, ``unknown`` where it does
    not.  An undated signal is still a signal -- "the next large deal that
    names AI in its scope" has no date and is exactly what a pathway is made
    of -- but a call whose every step is undated is a call nobody can check.
    """

    if not isinstance(value, list) or not value:
        raise ConvictionCallValidationError(
            f"{name} must name at least one observable signal; a call with no "
            "pathway is a wish")
    if len(value) > MAX_SIGNALS:
        raise ConvictionCallValidationError(
            f"{name} carries more than {MAX_SIGNALS} steps")
    steps = [_pathway_step(row, f"{name}[{index}]") for index, row in enumerate(value)]
    if len({step["signal"] for step in steps}) != len(steps):
        raise ConvictionCallValidationError(f"{name} repeats a signal")
    return steps


def _case(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_CASE_FIELDS), name)
    percent = wire["percent"]
    return {
        "statement": _text(wire["statement"], f"{name}.statement"),
        "percent": None if percent is None else str(_decimal(percent, f"{name}.percent")),
        "refs": _refs(wire["refs"], f"{name}.refs", nonempty=True),
    }


def validate_risk_reward(
    value: Any, *, direction: str, time_horizon: str, name: str = "risk_reward",
    policy: Mapping[str, Any] = CONVICTION_POLICY,
) -> dict[str, Any]:
    """Both cases with references, and the Playbook's verdict recomputed.

    The ``standard`` block is never taken from the caller.  It is derived here
    from the two percentages and the frozen table, so a drafter cannot assert
    that it cleared a bar it did not clear, and a stored call can be replayed
    against the same policy hash years later.
    """

    wire = _closed(value, set(_RISK_REWARD_FIELDS), name)
    upside = _case(wire["upside"], f"{name}.upside")
    downside = _case(wire["downside"], f"{name}.downside")
    standard = check_risk_reward(
        direction=direction, time_horizon=time_horizon,
        upside_percent=upside["percent"], downside_percent=downside["percent"],
        policy=policy,
    )
    supplied = wire["standard"]
    if supplied is not None:
        checked = _closed(supplied, set(_STANDARD_FIELDS), f"{name}.standard")
        if checked != standard:
            raise ConvictionCallValidationError(
                f"{name}.standard is derived from the policy and does not match "
                "what the policy says")
    return {"upside": upside, "downside": downside, "standard": standard}


def _falsifier(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_FALSIFIER_FIELDS), name)
    return {
        "statement": _text(wire["statement"], f"{name}.statement"),
        "falsifier_ref": _optional_text(wire["falsifier_ref"], f"{name}.falsifier_ref",
                                        maximum=512),
        "thesis_version_ref": _text(wire["thesis_version_ref"],
                                    f"{name}.thesis_version_ref", maximum=512),
    }


def _attribution(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    wire = _closed(value, set(_ATTRIBUTION_FIELDS), name)
    wire["kind"] = _one_of(wire["kind"], ("model", "deterministic"), f"{name}.kind")
    for field in ("work_order_ref", "invocation_ref", "route_decision_ref", "model_family"):
        wire[field] = _optional_text(wire[field], f"{name}.{field}", maximum=512)
    if wire["kind"] == "model" and wire["work_order_ref"] is None:
        raise ConvictionCallValidationError(
            f"{name} is a model draft and must name its work order")
    return wire


def cited_refs(record: Mapping[str, Any]) -> set[str]:
    """Every reference a call stands on."""

    refs: set[str] = set()
    variant = record.get("variant_view") or {}
    for key in ("our_view", "market_view", "where_market_is_wrong",
                "convergence_pathway"):
        refs.update((variant.get(key) or {}).get("refs") or [])
    for metric in (record.get("consensus_gap") or {}).get("metrics") or []:
        refs.update(metric.get("refs") or [])
    for step in record.get("event_pathway") or []:
        refs.update(step.get("refs") or [])
    reward = record.get("risk_reward") or {}
    for key in ("upside", "downside"):
        refs.update((reward.get(key) or {}).get("refs") or [])
    refs.update(record.get("thesis_refs") or [])
    return {ref for ref in refs if ref}


def normalise_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    """Every field of a proposal, checked and in its stored form.

    The body without the hash, because the hash is taken *over the normalised
    body*: the validators here deduplicate refs, strip whitespace and derive
    the risk/reward verdict from the frozen policy, and hashing what a caller
    passed in would mean the bytes that were hashed and the bytes that are
    stored were two different things.
    """

    if not isinstance(value, Mapping):
        raise ConvictionCallValidationError("ConvictionCallProposal must be an object")
    wire = dict(value)
    wire.pop("content_hash", None)
    fields = set(_PROPOSAL_FIELDS) - {"content_hash"}
    if set(wire) != fields:
        raise ConvictionCallValidationError(
            "ConvictionCallProposal has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConvictionCallValidationError(
            "unsupported ConvictionCallProposal schema_version")
    for field in ("id", "created_at", "call_ref", "company_ref", "week_key",
                  "evidence_fingerprint", "policy_ref", "mission_version_ref",
                  "actor_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    wire["policy_hash"] = _hash(wire["policy_hash"], "policy_hash")
    wire["mission_version_hash"] = _hash(
        wire["mission_version_hash"], "mission_version_hash")
    wire["direction"] = _one_of(wire["direction"], DIRECTIONS, "direction")
    wire["decision"] = _one_of(wire["decision"], DECISION_VOCABULARY, "decision")
    wire["confidence"] = _one_of(wire["confidence"], CONFIDENCES, "confidence")
    wire["time_horizon"] = _one_of(wire["time_horizon"], TIME_HORIZONS, "time_horizon")
    wire["checkpoint_kind"] = _one_of(
        wire["checkpoint_kind"], (CHECKPOINT_KIND,), "checkpoint_kind")
    if wire["call_ref"] != call_ref_for(wire["company_ref"]):
        raise ConvictionCallConflict("call_ref is not derived from the company")
    if wire["week_key"] != week_key(wire["created_at"]):
        raise ConvictionCallConflict("week_key is not the week the call was made in")

    wire["variant_view"] = validate_variant_view(wire["variant_view"])
    wire["consensus_gap"] = validate_consensus_gap(wire["consensus_gap"])
    wire["event_pathway"] = validate_event_pathway(wire["event_pathway"])
    wire["risk_reward"] = validate_risk_reward(
        wire["risk_reward"], direction=wire["direction"],
        time_horizon=wire["time_horizon"])

    falsifiers = wire["falsifiers"]
    if not isinstance(falsifiers, list) or not falsifiers:
        raise ConvictionCallValidationError(
            "falsifiers must name at least one thing that would prove us wrong")
    if len(falsifiers) > MAX_FALSIFIERS:
        raise ConvictionCallValidationError(
            f"falsifiers carries more than {MAX_FALSIFIERS} entries")
    wire["falsifiers"] = [
        _falsifier(row, f"falsifiers[{index}]")
        for index, row in enumerate(falsifiers)
    ]
    wire["thesis_refs"] = _refs(wire["thesis_refs"], "thesis_refs", nonempty=True)
    wire["debate_refs"] = _refs(wire["debate_refs"], "debate_refs")
    known = set(wire["thesis_refs"])
    for index, row in enumerate(wire["falsifiers"]):
        if row["thesis_version_ref"] not in known:
            # A falsifier belongs to a thesis.  One that names a thesis this
            # call does not stand on is a falsifier for some other argument.
            raise ConvictionCallValidationError(
                f"falsifiers[{index}] names a thesis this call does not cite")

    if not isinstance(wire["precheck"], Mapping):
        raise ConvictionCallValidationError("precheck must be the gate's own record")
    wire["precheck"] = dict(wire["precheck"])
    if not wire["precheck"].get("eligible"):
        raise ConvictionCallValidationError(
            "a call is only drafted for a company the gate admitted")

    rubric = _closed(wire["rubric"], set(_RUBRIC_FIELDS), "rubric")
    wire["rubric"] = {
        "rubric_ref": _text(rubric["rubric_ref"], "rubric.rubric_ref", maximum=256),
        "rubric_hash": _hash(rubric["rubric_hash"], "rubric.rubric_hash"),
        "findings": _refs(rubric["findings"], "rubric.findings"),
    }
    if wire["rubric"]["findings"]:
        raise ConvictionCallValidationError(
            "a call with open rubric findings is not proposed; fix or refuse it")

    wire["drafted_by"] = _attribution(wire["drafted_by"], "drafted_by")
    wire["verified_by"] = _attribution(wire["verified_by"], "verified_by")
    return wire


def validate_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one ConvictionCallProposal, hash included."""

    if not isinstance(value, Mapping) or "content_hash" not in value:
        raise ConvictionCallValidationError(
            "ConvictionCallProposal must be an object carrying its content_hash")
    body = normalise_proposal(value)
    expected = content_hash(body)
    if value["content_hash"] != expected:
        raise ConvictionCallConflict("ConvictionCallProposal content hash drifted")
    return {**body, "content_hash": expected}


def validate_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one decision on one proposal."""

    if not isinstance(value, Mapping):
        raise ConvictionCallValidationError("a decision must be an object")
    wire = dict(value)
    if set(wire) != set(_DECISION_FIELDS):
        raise ConvictionCallValidationError(
            "the decision has an invalid closed shape; "
            f"missing={sorted(set(_DECISION_FIELDS) - set(wire))}, "
            f"unknown={sorted(set(wire) - set(_DECISION_FIELDS))}")
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ConvictionCallValidationError("unsupported decision schema_version")
    for field in ("id", "created_at", "proposal_ref"):
        wire[field] = _text(wire[field], field, maximum=512)
    wire["proposal_hash"] = _hash(wire["proposal_hash"], "proposal_hash")
    wire["decision"] = _one_of(wire["decision"], DECISIONS, "decision")
    wire["reason"] = _text(wire["reason"], "reason", maximum=MAX_REASON_CHARS)
    wire["actor_ref"] = _human(wire["actor_ref"])
    number = wire["decision_number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ConvictionCallValidationError("decision_number must be a positive integer")
    body = {key: item for key, item in wire.items() if key != "content_hash"}
    if wire["content_hash"] != content_hash(body):
        raise ConvictionCallConflict("decision content hash drifted")
    return wire


# ---------------------------------------------------------------------------
# the Q1 rubric, checked mechanically
# ---------------------------------------------------------------------------

# The rubric's criteria that can be settled without reading anything: they are
# about whether the required blocks are there and internally consistent, not
# about whether the argument is any good.  The judge half of the rubric asks
# the second question; this half runs before a proposal is written, so a call
# that fails it is never put in front of a person.
#
# They live here rather than in ``research_quality_score.CHECKS`` because that
# registry is a shared file and this slice may only add to the rubric module.
# ``conviction_call_rubric_findings`` is named in the rubric's grading notes so
# a reader of the rubric knows where its mechanical half is.
def rubric_findings(record: Mapping[str, Any]) -> list[str]:
    """Every criterion this call mechanically fails, by criterion id."""

    findings: list[str] = []
    variant = record.get("variant_view") or {}
    market = variant.get("market_view") or {}
    wrong = variant.get("where_market_is_wrong") or {}
    if not market.get("available") or not (market.get("refs") or []) \
            or not (wrong.get("statement") or "").strip():
        findings.append("variant_view_is_variant")
    pathway = record.get("event_pathway") or []
    # Deliberately not "at least one step carries a date". The blueprint says
    # dated *when known*, and on a Core with no catalyst calendar nothing is
    # known -- a mechanical rule requiring a date would make a call impossible
    # rather than making it better. An undated pathway is the rubric's own
    # anchor for a 2; grading it is the judge's job, not this gate's.
    if not pathway or not all(step.get("refs") for step in pathway):
        findings.append("pathway_is_observable")
    gap = record.get("consensus_gap") or {}
    if gap.get("status") == "available":
        if not (gap.get("metrics") or []):
            findings.append("consensus_gap_named")
    elif not (gap.get("reason") or "").strip():
        findings.append("consensus_gap_named")
    standard = (record.get("risk_reward") or {}).get("standard") or {}
    if standard.get("status") not in RISK_REWARD_STATUSES:
        findings.append("risk_reward_against_the_standard")
    theses = set(record.get("thesis_refs") or [])
    falsifiers = record.get("falsifiers") or []
    if not falsifiers or not all(
            row.get("thesis_version_ref") in theses for row in falsifiers):
        findings.append("falsifiers_bound_to_a_thesis")
    if record.get("direction") == "short" and record.get("time_horizon") != "3_6_months":
        findings.append("horizon_matches_the_direction")
    return sorted(dict.fromkeys(findings))


def conviction_call_artefact(record: Mapping[str, Any]) -> dict[str, Any]:
    """One call in the shape ``research_quality_score`` grades.

    Not a second rendering of the object: the same blocks, flattened into the
    generic artefact the Q1 scorer already knows how to read, so that a call
    can be graded by the same machinery as an Initial Screen rather than by a
    scorer written for one object.
    """

    variant = record.get("variant_view") or {}
    market = variant.get("market_view") or {}
    reward = record.get("risk_reward") or {}
    standard = reward.get("standard") or {}
    gap = record.get("consensus_gap") or {}

    def block(title: str, body: str, refs: Sequence[str], gaps: Sequence[str] = ()) -> dict:
        return {"title": title, "body": body, "claim_refs": list(refs),
                "gaps": list(gaps), "numbers": []}

    pathway_lines = []
    for step in record.get("event_pathway") or []:
        window = step.get("window") or {}
        when = (window.get("date") or
                (f"{window.get('from')}..{window.get('to')}"
                 if window.get("kind") == "range" else "日期未知"))
        pathway_lines.append(f"{when}：{step.get('signal')}")
    if gap.get("status") == "available":
        gap_body = "；".join(
            f"{row['metric']} {row['period']}：我们 {row['ours']}{row['unit']}，"
            f"街上 {row['consensus']}{row['unit']}，差 {row['gap_percent']}%"
            for row in gap.get("metrics") or []
        )
        gap_gaps: list[str] = []
        gap_refs = [ref for row in gap.get("metrics") or [] for ref in row.get("refs") or []]
    else:
        gap_body = ""
        gap_gaps = [str(gap.get("reason") or "")]
        gap_refs = []
    sections = [
        block("我们的看法", (variant.get("our_view") or {}).get("statement") or "",
              (variant.get("our_view") or {}).get("refs") or []),
        block("市场的看法", market.get("statement") or "",
              market.get("refs") or [],
              [] if market.get("available") else [str(market.get("reason") or "")]),
        block("市场错在哪", (variant.get("where_market_is_wrong") or {}).get("statement") or "",
              (variant.get("where_market_is_wrong") or {}).get("refs") or []),
        block("靠拢路径", (variant.get("convergence_pathway") or {}).get("statement") or "",
              (variant.get("convergence_pathway") or {}).get("refs") or []),
        block("预期差", gap_body, gap_refs, gap_gaps),
        block("可观察信号", "\n".join(pathway_lines),
              [ref for step in record.get("event_pathway") or []
               for ref in step.get("refs") or []]),
        block("风险回报",
              f"上行：{(reward.get('upside') or {}).get('statement') or ''}\n"
              f"下行：{(reward.get('downside') or {}).get('statement') or ''}\n"
              f"对照 Playbook 标准：{standard.get('status')}——{standard.get('reason') or ''}",
              list((reward.get("upside") or {}).get("refs") or [])
              + [ref for ref in (reward.get("downside") or {}).get("refs") or []
                 if ref not in ((reward.get("upside") or {}).get("refs") or [])]),
        block("证伪条件",
              "；".join(row.get("statement") or "" for row in record.get("falsifiers") or []),
              list(record.get("thesis_refs") or [])),
    ]
    return {
        "artefact_kind": "conviction_call",
        "ref": record.get("id"),
        "hash": record.get("content_hash"),
        "title": f"{record.get('company_ref')} {record.get('direction')} "
                 f"({record.get('time_horizon')})",
        "subject_ref": record.get("company_ref"),
        "question": None,
        "confidence": record.get("confidence"),
        "prior": None,
        "sections": sections,
        "shown_claims": [],
        "cited_tags": [],
        "expected_sections": [section["title"] for section in sections],
    }


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------

def table_exists(connection: Any) -> bool:
    """Whether this Core has ever held a conviction call."""

    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (PROPOSAL_TABLE,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _decode_proposal(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ConvictionCallNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConvictionCallConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ConvictionCallConflict(f"{name} record_json is not canonical")
    record = validate_proposal(wire)
    if record["content_hash"] != row["content_hash"] or record["id"] != row["proposal_id"]:
        raise ConvictionCallConflict(f"{name} identity columns drifted")
    columns = {
        "call_ref": record["call_ref"],
        "company_ref": record["company_ref"],
        "week_key": record["week_key"],
        "direction": record["direction"],
        "decision_word": record["decision"],
        "confidence": record["confidence"],
        "time_horizon": record["time_horizon"],
        "risk_reward_status": record["risk_reward"]["standard"]["status"],
        "evidence_fingerprint": record["evidence_fingerprint"],
        "actor_ref": record["actor_ref"],
        "created_at": record["created_at"],
    }
    keys = set(row.keys())
    for column, expected in columns.items():
        if column in keys and row[column] != expected:
            raise ConvictionCallConflict(f"{name} column {column} drifted")
    return record


def _decode_decision(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ConvictionCallNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConvictionCallConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ConvictionCallConflict(f"{name} record_json is not canonical")
    record = validate_decision(wire)
    if record["content_hash"] != row["content_hash"] or record["id"] != row["decision_id"]:
        raise ConvictionCallConflict(f"{name} identity columns drifted")
    return record


class ConvictionCallAuthority:
    """Append-only proposals, and the append-only decisions made about them.

    The authority is a mechanism and nothing more.  ``propose`` requires the
    gate's own record and the exact evidence fingerprint from its caller, and
    there is no code path in this class that calls it; deciding *whether* a
    call is worth making belongs to ``conviction_call_draft`` and the lane.
    ``decide`` refuses anything but a ``human:`` principal, twice over -- here
    and in the schema -- because the one place a person's judgement enters the
    record should not depend on a single gate.
    """

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ConvictionCallAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reads ------------------------------------------------------------

    def proposal(self, proposal_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT * FROM {PROPOSAL_TABLE} WHERE proposal_id=?",
            (_text(proposal_ref, "proposal_ref", maximum=512),),
        ).fetchone()
        return _decode_proposal(row, f"ConvictionCallProposal {proposal_ref}")

    def proposals(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        if company_ref is None:
            rows = self.connection.execute(
                f"SELECT * FROM {PROPOSAL_TABLE} ORDER BY created_at, proposal_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                f"SELECT * FROM {PROPOSAL_TABLE} WHERE company_ref=? "
                "ORDER BY created_at, proposal_id",
                (_text(company_ref, "company_ref", maximum=256),),
            ).fetchall()
        return [_decode_proposal(row, "ConvictionCallProposal") for row in rows]

    def decisions(self, proposal_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT * FROM {DECISION_TABLE} WHERE proposal_ref=? "
            "ORDER BY decision_number",
            (_text(proposal_ref, "proposal_ref", maximum=512),),
        ).fetchall()
        return [_decode_decision(row, "conviction call decision") for row in rows]

    def latest_decision(self, proposal_ref: str) -> dict[str, Any] | None:
        chain = self.decisions(proposal_ref)
        return chain[-1] if chain else None

    def status_of(self, proposal_ref: str) -> str:
        """``open``, ``deferred``, ``accepted`` or ``rejected``."""

        latest = self.latest_decision(proposal_ref)
        if latest is None:
            return "open"
        return {"accept": "accepted", "reject": "rejected",
                "defer": "deferred"}[latest["decision"]]

    def open_calls(self) -> list[dict[str, Any]]:
        """Every proposal nobody has answered yet, oldest first.

        What the Cockpit's approvals page lists.  A deferred call is not here:
        deferring is an answer -- "not now" -- and a queue that re-raised it
        every tick would teach the owner to ignore the queue.
        """

        rows = self.connection.execute(
            f"SELECT p.* FROM {PROPOSAL_TABLE} p LEFT JOIN {DECISION_TABLE} d "
            "ON d.proposal_ref = p.proposal_id WHERE d.decision_id IS NULL "
            "ORDER BY p.created_at, p.proposal_id"
        ).fetchall()
        return [_decode_proposal(row, "ConvictionCallProposal") for row in rows]

    def deferred_calls(self) -> list[dict[str, Any]]:
        """The ones put off, which a person may still come back to."""

        return [
            record for record in self.proposals()
            if self.status_of(record["id"]) == "deferred"
        ]

    def accepted_calls(
        self, window: tuple[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """The calls a person accepted, for P15c's weekly brief.

        ``window`` is ``(since, until)`` over the *decision's* timestamp --
        half open, since inclusive -- because the question the brief asks is
        "what did we decide this week", not "what did the machine draft".  A
        call proposed in August and accepted this morning belongs in this
        week's brief.
        """

        since = until = None
        if window is not None:
            if not isinstance(window, (tuple, list)) or len(window) != 2:
                raise ConvictionCallValidationError(
                    "window is (since, until) as ISO timestamps")
            since = _text(window[0], "window.since", maximum=64)
            until = _text(window[1], "window.until", maximum=64)
        found: list[dict[str, Any]] = []
        for record in self.proposals():
            latest = self.latest_decision(record["id"])
            if latest is None or latest["decision"] != "accept":
                continue
            when = latest["created_at"]
            if since is not None and when < since:
                continue
            if until is not None and when >= until:
                continue
            found.append({**record, "decision_record": latest})
        found.sort(key=lambda item: (item["decision_record"]["created_at"], item["id"]))
        return found

    def calls_this_week(self, company_ref: str, when: Any) -> list[dict[str, Any]]:
        """This company's proposals in the ISO week a moment falls in."""

        rows = self.connection.execute(
            f"SELECT * FROM {PROPOSAL_TABLE} WHERE company_ref=? AND week_key=? "
            "ORDER BY created_at",
            (_text(company_ref, "company_ref", maximum=256), week_key(when)),
        ).fetchall()
        return [_decode_proposal(row, "ConvictionCallProposal") for row in rows]

    def counts(self) -> dict[str, int]:
        proposals = self.connection.execute(
            f"SELECT COUNT(*) FROM {PROPOSAL_TABLE}").fetchone()[0]
        decisions = self.connection.execute(
            f"SELECT COUNT(*) FROM {DECISION_TABLE}").fetchone()[0]
        return {"proposals": int(proposals), "decisions": int(decisions)}

    # -- the writes -------------------------------------------------------

    def propose(
        self,
        *,
        company_ref: str,
        direction: str,
        decision: str,
        confidence: str,
        time_horizon: str,
        variant_view: Mapping[str, Any],
        consensus_gap: Mapping[str, Any],
        event_pathway: Sequence[Mapping[str, Any]],
        risk_reward: Mapping[str, Any],
        falsifiers: Sequence[Mapping[str, Any]],
        thesis_refs: Sequence[str],
        debate_refs: Sequence[str] = (),
        evidence_fingerprint: str,
        precheck_record: Mapping[str, Any],
        rubric: Mapping[str, Any],
        mission: Mapping[str, Any],
        actor_ref: str,
        created_at: str,
        drafted_by: Mapping[str, Any] | None = None,
        verified_by: Mapping[str, Any] | None = None,
        max_per_week: int | None = None,
    ) -> dict[str, Any]:
        """Write one proposal, or refuse it.

        Three refusals and they are different: ``duplicate`` when this exact
        evidence already produced a call for this company, ``rate_limited``
        when the week's allowance is spent, and a raised validation error when
        the record is not a call at all.  The first two are answers a lane
        reports; the third is a bug.
        """

        company_ref = _text(company_ref, "company_ref", maximum=256)
        created_at = _text(created_at, "created_at", maximum=64)
        fingerprint = _text(evidence_fingerprint, "evidence_fingerprint", maximum=128)
        allowance = (CONVICTION_POLICY["max_calls_per_company_per_week"]
                     if max_per_week is None else int(max_per_week))
        existing = self.connection.execute(
            f"SELECT * FROM {PROPOSAL_TABLE} WHERE company_ref=? AND evidence_fingerprint=?",
            (company_ref, fingerprint),
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate",
                    "reason": "this company's evidence has already produced a call",
                    **_decode_proposal(existing, "ConvictionCallProposal")}
        week = week_key(created_at)
        already = self.connection.execute(
            f"SELECT COUNT(*) FROM {PROPOSAL_TABLE} WHERE company_ref=? AND week_key=?",
            (company_ref, week),
        ).fetchone()[0]
        if int(already) >= allowance:
            return {"status": "rate_limited",
                    "reason": (f"{company_ref} already has {already} call(s) in "
                               f"{week}; the allowance is {allowance}"),
                    "week_key": week, "company_ref": company_ref}

        body = {
            "call_ref": call_ref_for(company_ref),
            "company_ref": company_ref,
            "week_key": week,
            "direction": direction,
            "decision": decision,
            "confidence": confidence,
            "time_horizon": time_horizon,
            "variant_view": dict(variant_view),
            "consensus_gap": dict(consensus_gap),
            "event_pathway": [dict(item) for item in event_pathway],
            "risk_reward": dict(risk_reward),
            "falsifiers": [dict(item) for item in falsifiers],
            "thesis_refs": list(thesis_refs),
            "debate_refs": list(debate_refs),
            "evidence_fingerprint": fingerprint,
            "precheck": dict(precheck_record),
            "rubric": dict(rubric),
            "drafted_by": None if drafted_by is None else dict(drafted_by),
            "verified_by": None if verified_by is None else dict(verified_by),
            "policy_ref": POLICY_REF,
            "policy_hash": POLICY_HASH,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "checkpoint_kind": CHECKPOINT_KIND,
            "actor_ref": _text(actor_ref, "actor_ref", maximum=256),
        }
        identity = {"company_ref": company_ref, "evidence_fingerprint": fingerprint,
                    "week_key": week}
        wire = normalise_proposal({
            "schema_version": SCHEMA_VERSION,
            "id": "conviction-call-proposal:" + content_hash(identity)[:32],
            "created_at": created_at,
            **body,
        })
        record = {**wire, "content_hash": content_hash(wire)}
        with self.store._transaction() as cur:
            cur.execute(
                f"INSERT INTO {PROPOSAL_TABLE}(proposal_id,call_ref,company_ref,week_key,"
                "direction,decision_word,confidence,time_horizon,risk_reward_status,"
                "evidence_fingerprint,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record["id"], record["call_ref"], record["company_ref"],
                    record["week_key"], record["direction"], record["decision"],
                    record["confidence"], record["time_horizon"],
                    record["risk_reward"]["standard"]["status"],
                    record["evidence_fingerprint"], canonical_json(record),
                    record["content_hash"], record["actor_ref"], record["created_at"],
                ),
            )
        stored = self.proposal(record["id"])
        if stored["content_hash"] != record["content_hash"]:
            raise ConvictionCallConflict("the stored proposal did not read back")
        return {"status": "fresh", "reason": None, **stored}

    def decide(
        self,
        *,
        proposal_ref: str,
        proposal_hash: str,
        decision: str,
        reason: str,
        actor_ref: str,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """A person answers one call.  Append-only, bound to the exact bytes.

        ``accept`` and ``reject`` settle it; a second decision on a settled
        call is a conflict rather than a correction, because "we accepted it
        and then quietly did not" is precisely the history this record exists
        to make unavailable.  ``defer`` leaves it open, and the chain shows
        both the pause and what ended it.
        """

        proposal_ref = _text(proposal_ref, "proposal_ref", maximum=512)
        decision = _one_of(decision, DECISIONS, "decision")
        actor_ref = _human(actor_ref)
        proposal = self.proposal(proposal_ref)
        if proposal["content_hash"] != _hash(proposal_hash, "proposal_hash"):
            raise ConvictionCallConflict(
                "the decision names a different version of this call than the "
                "one stored; nothing here is rewritten, so this is a stale read")
        chain = self.decisions(proposal_ref)
        if chain and chain[-1]["decision"] in SETTLING_DECISIONS:
            raise ConvictionCallConflict(
                f"{proposal_ref} was already {chain[-1]['decision']}ed")
        number = len(chain) + 1
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": "conviction-call-decision:" + content_hash(
                {"proposal_ref": proposal_ref, "decision_number": number})[:32],
            "created_at": _text(created_at or _now(), "created_at", maximum=64),
            "proposal_ref": proposal_ref,
            "proposal_hash": proposal["content_hash"],
            "decision_number": number,
            "decision": decision,
            "reason": _text(reason, "reason", maximum=MAX_REASON_CHARS),
            "actor_ref": actor_ref,
        }
        record["content_hash"] = content_hash(record)
        checked = validate_decision(record)
        with self.store._transaction() as cur:
            cur.execute(
                f"INSERT INTO {DECISION_TABLE}(decision_id,proposal_ref,proposal_hash,"
                "decision_number,decision,reason,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    checked["id"], checked["proposal_ref"], checked["proposal_hash"],
                    checked["decision_number"], checked["decision"], checked["reason"],
                    canonical_json(checked), checked["content_hash"],
                    checked["actor_ref"], checked["created_at"],
                ),
            )
        written = _decode_decision(
            self.connection.execute(
                f"SELECT * FROM {DECISION_TABLE} WHERE decision_id=?", (checked["id"],),
            ).fetchone(),
            "conviction call decision",
        )
        if written["content_hash"] != checked["content_hash"]:
            raise ConvictionCallConflict("the decision did not read back")
        return {"status": "recorded", **written,
                "call_status": self.status_of(proposal_ref)}


__all__ = [
    "CHECKPOINT_KIND",
    "CONFIDENCES",
    "CONVICTION_POLICY",
    "DECISIONS",
    "DECISION_TABLE",
    "DIRECTIONS",
    "GATE_REASONS",
    "MARKET_VIEW_SOURCES",
    "POLICY_HASH",
    "POLICY_REF",
    "PROPOSAL_TABLE",
    "RISK_REWARD_STATUSES",
    "SCHEMA_VERSION",
    "SETTLING_DECISIONS",
    "TIME_HORIZONS",
    "WINDOW_KINDS",
    "WRITE_SCOPE",
    "ConvictionCallAuthority",
    "ConvictionCallConflict",
    "ConvictionCallError",
    "ConvictionCallNotFound",
    "ConvictionCallValidationError",
    "call_ref_for",
    "check_risk_reward",
    "cited_refs",
    "conviction_call_artefact",
    "divergent_debates",
    "evidence_fingerprint",
    "normalise_proposal",
    "precheck",
    "rubric_findings",
    "standard_for",
    "table_exists",
    "validate_consensus_gap",
    "validate_decision",
    "validate_event_pathway",
    "validate_proposal",
    "validate_risk_reward",
    "validate_variant_view",
    "variant_material",
    "week_key",
    "wide_consensus_gaps",
]
