"""P12c: where we differ from the market, and what would settle it.

Everything else this system builds is a record of what is true.  A debate map
is the record of what is *contested*, which is a different and smaller thing:
a question two sets of evidence answer differently, the strongest claim on
each side, where consensus stands, where we stand, and -- when it moves --
which side has been gaining and on what.

The owner's rule shapes the contract more than the blueprint does: agreeing
with the market has no value.  So a debate that does not say where the market
is and where we are is not a debate, it is a literature review; the two
positions are separate required fields and a version cannot be published
without them, even when the honest answer is "the market has not spoken here"
(``available: false``) or "we have no view yet" (``none_yet``).

Three things live in this module and nowhere else.

**The contract.**  One ``DebateMapVersion`` per subject per version, append
only, content hashed, three ``dalton_authorized()`` triggers, read back after
the write, ``duplicate`` when nothing was learned -- the shape every authority
here follows, and ADR-0008's rules on top: a version names its
``change_reason`` and the exact refs that occasioned it, and a revision whose
refs the current version already cites is refused rather than written.

**The constitution gate.**  Every candidate debate is screened *before* a word
of it is drafted, against the active constitution's ``method``: it must bind a
driver that exists, cite evidence on both sides, and name which admission rule
and which link of the causal chain it sits on.  The check is entirely
deterministic -- ref membership and list indices, no judgement -- and every
refusal is recorded on the version with its reason, because a gate whose
rejections vanish is a gate nobody can audit.

**Source independence.**  Two notes from the same broker are one source.  The
Ledger's own ``independence_group`` is per document and would count them as
two, so this module derives a publisher identity from a frozen table and folds
documents by it.  A claim whose publisher cannot be established is listed and
does not count: under-counting keeps a thin debate at ``candidate``, and
over-counting would promote it to ``open`` on one broker's opinion twice.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .claim_index_tagging import fold
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

_SCHEMA_PATH = Path(__file__).with_name("debate_map_schema.sql")

TABLE = "debate_map_versions"

# ADR-0008's closed vocabulary.  A version that cannot name why it exists is a
# rewrite, and a rewrite of an unchanged world is noise wearing a version
# number.
CHANGE_REASONS: tuple[str, ...] = (
    "filing_actual", "driver_event", "assumption_review",
    "evidence_thicker", "human_revision",
)

# ``candidate`` is not in the blueprint's three and is the most important of
# the four.  A question with evidence on both sides but only one broker behind
# each is a real question and a bad debate; recording it as ``open`` would put
# it in the weekly brief next to debates two independent houses disagree
# about.  So it is admitted, kept, and marked as what it is.
DEBATE_STATUSES: tuple[str, ...] = ("candidate", "open", "shifting", "resolved")
# The statuses a reader means by "what is still being argued about".
LIVE_STATUSES: frozenset[str] = frozenset({"open", "shifting"})

SUBJECT_KINDS: tuple[str, ...] = ("company", "industry")

MARKET_LEANS: tuple[str, ...] = ("bull", "bear", "split")
OUR_SIDES: tuple[str, ...] = ("bull", "bear", "neither")
GAINING: tuple[str, ...] = ("bull", "bear", "neither")

# Why a candidate debate never became one.  Each is a mechanical check with a
# single cause, so a rejection tells the reader what to fix rather than that
# something was wrong.
GATE_REASONS: tuple[str, ...] = (
    # the question itself
    "empty_question",
    "empty_position",
    # the driver binding: a debate that hangs off no driver cannot move a
    # forecast, and one that names a driver nobody has heard of is a debate
    # about a thing this system does not model
    "no_driver_binding",
    "unknown_driver",
    # both sides need evidence, and it has to be evidence that was shown
    "no_bull_evidence",
    "no_bear_evidence",
    "unknown_claim_ref",
    # the constitution's method: which admission rule admits this question, and
    # which link of the causal chain it sits on
    "no_question_admission_rule",
    "unknown_question_admission_rule",
    "no_causal_chain_link",
    "unknown_causal_chain_link",
)


# ---------------------------------------------------------------------------
# the frozen policy
# ---------------------------------------------------------------------------

# Everything the deterministic layers read is in here rather than scattered
# through the code, for the reason every derived quantity in this repository
# is: a threshold that lives in an ``if`` cannot be replayed, and a debate that
# was ``open`` in September has to still be explicable in December.  The same
# bytes are published as ``deploy/phase9/p12c-debate-policy-v1.json`` and a
# test asserts the two have not drifted.
DEBATE_POLICY: Mapping[str, Any] = {
    "policy_ref": "debate-policy:p12c:v1",
    # A side needs at least one claim to exist and two independent sources to
    # be called open.  One broker saying it twice is one opinion.
    "min_claims_per_side": 1,
    "min_independent_sources_per_side": 2,
    # Strongest first.  The tier is a label on the input table the drafter
    # reads, so that "management says demand is fine" and "a message board
    # says demand is fine" never look like the same evidence.
    "source_tiers": [
        "filing", "management", "sell_side", "expert",
        "sales_note", "news", "crowd", "other",
    ],
    # P12b already decided what a document is worth; this only renames its
    # five tiers into the seven the debate table labels.
    "importance_tier": {
        "filing": "filing",
        "management_statement": "management",
        "sell_side": "sell_side",
        "news": "news",
        "other": "other",
    },
    # The discovery spec is the only thing that tells an earnings call and a
    # broker note apart when both arrive through the same connector, so where
    # a spec is known it wins over the importance tier.
    "spec_tier": {
        "earnings-call-transcripts": "management",
        "sell-side-reports": "sell_side",
        "company-wiki": "expert",
        "guidepoint": "expert",
        "sales-notes": "sales_note",
        "xueqiu": "crowd",
        "x-xreach": "crowd",
        "employee-reviews": "crowd",
        "management-changes": "news",
        "industry-demand": "news",
        "competitive-landscape": "news",
    },
    # Publisher identity, longest pattern first at match time.  This is the
    # whole of "two TD notes are one source": the Ledger's independence_group
    # is per document and would say two.
    "publishers": [
        {"publisher": "td", "tier": "sell_side",
         "patterns": ["td cowen", "td securities", "td bank"]},
        {"publisher": "wells-fargo", "tier": "sell_side",
         "patterns": ["wells fargo"]},
        {"publisher": "deutsche-bank", "tier": "sell_side",
         "patterns": ["deutsche bank", "deutsche"]},
        {"publisher": "wolfe", "tier": "sell_side",
         "patterns": ["wolfe research", "wolfe"]},
        {"publisher": "jpmorgan", "tier": "sell_side",
         "patterns": ["j.p. morgan", "jp morgan", "jpmorgan"]},
        {"publisher": "morgan-stanley", "tier": "sell_side",
         "patterns": ["morgan stanley"]},
        {"publisher": "hsbc", "tier": "sell_side", "patterns": ["hsbc"]},
        {"publisher": "rbc", "tier": "sell_side",
         "patterns": ["rbc capital", "rbc"]},
        {"publisher": "bernstein", "tier": "sell_side",
         "patterns": ["bernstein"]},
        {"publisher": "ubs", "tier": "sell_side", "patterns": ["ubs"]},
        {"publisher": "barclays", "tier": "sell_side", "patterns": ["barclays"]},
        {"publisher": "citi", "tier": "sell_side",
         "patterns": ["citigroup", "citi research"]},
        {"publisher": "goldman", "tier": "sell_side",
         "patterns": ["goldman sachs", "goldman"]},
        {"publisher": "bofa", "tier": "sell_side",
         "patterns": ["bofa", "bank of america", "merrill"]},
    ],
    # The pre-pass lexicon.  Deliberately short and deliberately missing the
    # words that read both ways in this industry -- "contract" is a bear word
    # everywhere except IT services, where a large contract is the bull case --
    # because a cue that fires on both sides finds debates that are not there.
    "positive_cues": [
        "accelerat", "beat", "expand", "improv", "strong", "upside",
        "tailwind", "outperform", "upgrade", "raise", "raised", "robust",
        "better than", "ahead of",
    ],
    "negative_cues": [
        "decelerat", "miss", "deteriorat", "weak", "downside", "headwind",
        "underperform", "downgrade", "cut", "lower", "decline", "soft",
        "pressure", "worse than", "below expectations",
    ],
}

POLICY_REF: str = str(DEBATE_POLICY["policy_ref"])
POLICY_HASH: str = content_hash(DEBATE_POLICY)


class DebateMapError(RuntimeError):
    """Base error for the debate map authority."""


class DebateMapValidationError(DebateMapError, ValueError):
    """A version does not satisfy the closed contract."""


class DebateMapConflict(DebateMapError):
    """Stored bytes disagree with themselves or with the request."""


class DebateMapNotFound(DebateMapError, LookupError):
    """No such map or version."""


# ---------------------------------------------------------------------------
# small validators
# ---------------------------------------------------------------------------

def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DebateMapValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise DebateMapValidationError(f"{name} must be lowercase SHA-256")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise DebateMapValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}"
        )
    return str(value)


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise DebateMapValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise DebateMapValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise DebateMapValidationError(f"{name} must contain unique refs")
    return result


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DebateMapValidationError(f"{name} must be an object")
    wire = dict(value)
    if set(wire) != fields:
        raise DebateMapValidationError(
            f"{name} has an invalid closed shape; "
            f"missing={sorted(fields - set(wire))}, "
            f"unknown={sorted(set(wire) - fields)}"
        )
    return wire


# ---------------------------------------------------------------------------
# source identity and independence
# ---------------------------------------------------------------------------

# What a source key was derived from, strongest first.  Recorded beside every
# key because "two sources" derived from two publishers and "two sources"
# derived from two URLs are not the same assertion.
SOURCE_BASES: tuple[str, ...] = (
    "publisher",      # a named house from the frozen table
    "issuer",         # the company itself: it speaks with one voice
    "host",           # a web host
    "document",       # two claims out of one document are one source
    "unattributed",   # nothing could be established; does not count
)

# The Ledger's own ``independence_group`` is deliberately *not* a rung.  It
# looked like the right answer -- it is on every evidence relation and it has
# the right name -- until the live Core was read: its three values there are
# ``independence:source:public-web``, ``independence:source:alphaengine`` and
# ``independence:source:sec-edgar``.  It groups by *connector*.  Using it would
# assert that two unrelated newspapers are one source because both arrived
# through web search, which is as wrong as asserting they are two.
# "We cannot tell" is the true answer and is what ``unattributed`` says.


def _publisher_patterns(policy: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """``(pattern, publisher, tier)`` ordered longest pattern first."""

    rows: list[tuple[str, str, str]] = []
    for entry in policy["publishers"]:
        for pattern in entry["patterns"]:
            rows.append((str(pattern), str(entry["publisher"]), str(entry["tier"])))
    rows.sort(key=lambda row: (-len(row[0]), row[0]))
    return rows


def publisher_of(text: Any, policy: Mapping[str, Any] = DEBATE_POLICY) -> str | None:
    """The house that published a document, from its title and ref.

    Longest pattern wins, so ``Morgan Stanley`` is not read as ``jp morgan``
    because one of them happens to be a substring of a longer name.
    """

    folded = fold(text)
    if not folded:
        return None
    for pattern, publisher, _tier in _publisher_patterns(policy):
        if pattern in folded:
            return publisher
    return None


def _host_of(document_ref: Any) -> str | None:
    ref = document_ref if isinstance(document_ref, str) else ""
    marker = "://"
    if marker not in ref:
        return None
    rest = ref.split(marker, 1)[1]
    host = rest.split("/", 1)[0].split("@")[-1].split(":")[0].strip().lower()
    return host or None


def source_identity(
    row: Mapping[str, Any], policy: Mapping[str, Any] = DEBATE_POLICY
) -> dict[str, Any]:
    """Who said this, for the purpose of counting how many people did.

    The ladder is strict and stops at the first rung that answers.  It never
    falls through to the claim itself as a *source*: an unattributed claim is
    evidence and is shown, but it cannot be one of the two independent voices
    that turn a candidate into an open debate.
    """

    tier = tier_of(row, policy)
    title = " ".join(
        str(row.get(field) or "")
        for field in ("document_title", "document_ref", "source_ref")
    )
    publisher = publisher_of(title, policy)
    if publisher is not None:
        return {"key": f"publisher:{publisher}", "basis": "publisher",
                "publisher": publisher, "tier": tier}
    if tier in ("filing", "management"):
        # The issuer is one voice however many filings and calls it holds.
        issuer = row.get("subject_ref")
        if isinstance(issuer, str) and issuer.strip():
            return {"key": f"issuer:{issuer.strip()}", "basis": "issuer",
                    "publisher": issuer.strip(), "tier": tier}
    host = row.get("host")
    if not (isinstance(host, str) and host.strip()):
        host = _host_of(row.get("document_ref"))
    if host:
        host = host.strip().lower()
        return {"key": f"host:{host}", "basis": "host", "publisher": host,
                "tier": tier}
    document_ref = row.get("document_ref")
    if isinstance(document_ref, str) and document_ref.strip():
        # The weakest rung that still says something true: one document is one
        # source, so a broker quoted three times in its own note is quoted
        # once.  It cannot see that two notes came from the same house -- that
        # needs the title, which is the rung above.
        return {"key": f"document:{document_ref.strip()}", "basis": "document",
                "publisher": None, "tier": tier}
    return {"key": None, "basis": "unattributed", "publisher": None, "tier": tier}


def tier_of(row: Mapping[str, Any], policy: Mapping[str, Any] = DEBATE_POLICY) -> str:
    """The source tier of one claim row: the spec if known, else importance."""

    spec = row.get("spec_ref")
    if isinstance(spec, str) and spec in policy["spec_tier"]:
        return str(policy["spec_tier"][spec])
    importance = row.get("importance")
    if isinstance(importance, str) and importance in policy["importance_tier"]:
        return str(policy["importance_tier"][importance])
    return "other"


def index_claims(
    rows: Iterable[Mapping[str, Any]], policy: Mapping[str, Any] = DEBATE_POLICY
) -> dict[str, dict[str, Any]]:
    """Claim rows keyed by ``claim_version_ref``, each with tier and source."""

    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        ref = row.get("claim_version_ref")
        if not isinstance(ref, str) or not ref.strip():
            continue
        identity = source_identity(row, policy)
        indexed[ref.strip()] = {**dict(row), **identity, "claim_version_ref": ref.strip()}
    return indexed


def count_independent_sources(
    claim_refs: Iterable[str],
    claims: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """How many distinct voices a side has, and which they are.

    Unattributed claims are counted separately and never as sources.  This is
    the conservative direction on purpose: the failure it prevents is a debate
    promoted to ``open`` because one broker was quoted twice.
    """

    keys: list[str] = []
    unattributed = 0
    for ref in claim_refs:
        row = claims.get(ref)
        if row is None:
            continue
        key = row.get("key")
        if not isinstance(key, str) or not key:
            unattributed += 1
            continue
        if key not in keys:
            keys.append(key)
    return {"count": len(keys), "keys": keys, "unattributed": unattributed}


# ---------------------------------------------------------------------------
# the deterministic pre-pass
# ---------------------------------------------------------------------------

def polarity(text: Any, policy: Mapping[str, Any] = DEBATE_POLICY) -> str:
    """``bull``, ``bear`` or ``neutral`` from a frozen cue lexicon.

    A statement carrying cues from both sides is ``neutral``, not "mixed":
    the pre-pass exists to find questions worth asking a model about, and a
    sentence that argues with itself is not evidence for either side of one.
    """

    folded = fold(text)
    positive = any(cue in folded for cue in policy["positive_cues"])
    negative = any(cue in folded for cue in policy["negative_cues"])
    if positive == negative:
        return "neutral"
    return "bull" if positive else "bear"


def contested_aspects(
    rows: Iterable[Mapping[str, Any]],
    policy: Mapping[str, Any] = DEBATE_POLICY,
) -> list[dict[str, Any]]:
    """Aspects where the evidence already disagrees with itself.

    No model call.  This is the seed list a drafting run reasons over and the
    number the smoke test reports: how many questions the Claims we hold
    already contain, before anyone is paid to find one.

    It groups by P12b's ``index_aspect`` rather than by driver on purpose.
    Binding a question to a driver is a judgement -- it is the first thing the
    constitution gate then checks -- and a rule that guessed at it would be
    inventing the very thing the gate is there to verify.
    """

    claims = index_claims(rows, policy)
    groups: dict[str, dict[str, list[str]]] = {}
    for ref, row in claims.items():
        aspect = row.get("index_aspect") or row.get("aspect")
        if not isinstance(aspect, str) or not aspect.strip():
            continue
        side = polarity(row.get("normalized_statement"), policy)
        if side == "neutral":
            continue
        groups.setdefault(aspect.strip(), {"bull": [], "bear": []})[side].append(ref)
    result: list[dict[str, Any]] = []
    minimum = int(policy["min_claims_per_side"])
    for aspect in sorted(groups):
        sides = groups[aspect]
        if len(sides["bull"]) < minimum or len(sides["bear"]) < minimum:
            continue
        bull = count_independent_sources(sorted(sides["bull"]), claims)
        bear = count_independent_sources(sorted(sides["bear"]), claims)
        result.append({
            "aspect": aspect,
            "bull_claim_refs": sorted(sides["bull"]),
            "bear_claim_refs": sorted(sides["bear"]),
            "bull_sources": bull["count"],
            "bear_sources": bear["count"],
            "tiers": sorted({
                claims[ref]["tier"]
                for ref in sides["bull"] + sides["bear"]
            }),
            "would_open": (
                bull["count"] >= int(policy["min_independent_sources_per_side"])
                and bear["count"] >= int(policy["min_independent_sources_per_side"])
            ),
        })
    return result


# ---------------------------------------------------------------------------
# the constitution gate (C3)
# ---------------------------------------------------------------------------

CANDIDATE_FIELDS = frozenset({
    "debate_ref", "question", "driver_refs", "question_admission_index",
    "causal_chain_index", "bull", "bear", "market", "ours", "gaining",
    "resolution",
})


def screen_candidate(
    candidate: Mapping[str, Any],
    *,
    method: Mapping[str, Any],
    driver_refs: Iterable[str],
    claims: Mapping[str, Mapping[str, Any]],
    known_debate_refs: Iterable[str] = (),
    policy: Mapping[str, Any] = DEBATE_POLICY,
) -> dict[str, Any]:
    """Decide whether one candidate debate may be published, and as what.

    Deterministic throughout.  Every check is either "is this ref in that set"
    or "is this index in range"; nothing here reads a sentence and forms an
    opinion about it, because a gate that formed opinions would be a second
    drafter with no verifier.

    Returns ``{"admitted": bool, "reasons": [...], "status": ...,
    "source_independence": {...}}``.  ``reasons`` is ordered as
    :data:`GATE_REASONS` is, so a rejection reads the same way twice.
    """

    reasons: set[str] = set()
    known_drivers = {str(ref) for ref in driver_refs}
    known = {str(ref) for ref in known_debate_refs}

    question = candidate.get("question")
    if not isinstance(question, str) or not question.strip():
        reasons.add("empty_question")

    bound = candidate.get("driver_refs")
    bound_list = [item for item in bound if isinstance(item, str) and item.strip()] \
        if isinstance(bound, list) else []
    if not bound_list:
        reasons.add("no_driver_binding")
    elif not set(bound_list) <= known_drivers:
        reasons.add("unknown_driver")

    sides: dict[str, list[str]] = {}
    for side, missing_reason in (("bull", "no_bull_evidence"), ("bear", "no_bear_evidence")):
        position = candidate.get(side)
        statement = position.get("statement") if isinstance(position, Mapping) else None
        if not isinstance(statement, str) or not statement.strip():
            reasons.add("empty_position")
        raw = position.get("claim_refs") if isinstance(position, Mapping) else None
        refs = [item for item in raw if isinstance(item, str) and item.strip()] \
            if isinstance(raw, list) else []
        sides[side] = refs
        if len(refs) < int(policy["min_claims_per_side"]):
            reasons.add(missing_reason)
        if not set(refs) <= set(claims):
            reasons.add("unknown_claim_ref")

    admission = method.get("question_admission") or []
    chain = method.get("causal_chain") or []
    for value, empty_reason, unknown_reason, table in (
        (candidate.get("question_admission_index"), "no_question_admission_rule",
         "unknown_question_admission_rule", admission),
        (candidate.get("causal_chain_index"), "no_causal_chain_link",
         "unknown_causal_chain_link", chain),
    ):
        if value is None:
            reasons.add(empty_reason)
        elif isinstance(value, bool) or not isinstance(value, int):
            reasons.add(unknown_reason)
        elif not 0 <= value < len(table):
            reasons.add(unknown_reason)

    bull = count_independent_sources(sides.get("bull", []), claims)
    bear = count_independent_sources(sides.get("bear", []), claims)
    independence = {
        "bull_sources": bull["count"], "bear_sources": bear["count"],
        "bull_source_keys": bull["keys"], "bear_source_keys": bear["keys"],
        "bull_unattributed": bull["unattributed"],
        "bear_unattributed": bear["unattributed"],
    }
    ordered = [reason for reason in GATE_REASONS if reason in reasons]
    if ordered:
        return {"admitted": False, "reasons": ordered, "status": None,
                "source_independence": independence}

    minimum = int(policy["min_independent_sources_per_side"])
    resolution = candidate.get("resolution")
    resolved = (
        isinstance(resolution, Mapping)
        and isinstance(resolution.get("refs"), list)
        and any(isinstance(item, str) and item.strip() for item in resolution["refs"])
    )
    if resolved:
        status = "resolved"
    elif bull["count"] >= minimum and bear["count"] >= minimum:
        gaining = candidate.get("gaining")
        status = (
            "shifting"
            if gaining in ("bull", "bear")
            and str(candidate.get("debate_ref") or "") in known
            else "open"
        )
    else:
        status = "candidate"
    return {"admitted": True, "reasons": [], "status": status,
            "source_independence": independence}


def screen_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    method: Mapping[str, Any],
    driver_refs: Iterable[str],
    claims: Mapping[str, Mapping[str, Any]],
    known_debate_refs: Iterable[str] = (),
    policy: Mapping[str, Any] = DEBATE_POLICY,
    observed_at: str,
) -> dict[str, list[dict[str, Any]]]:
    """Screen a whole draft, keeping the refusals."""

    drivers = list(driver_refs)
    known = list(known_debate_refs)
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        verdict = screen_candidate(
            candidate, method=method, driver_refs=drivers, claims=claims,
            known_debate_refs=known, policy=policy,
        )
        if verdict["admitted"]:
            admitted.append({"candidate": dict(candidate), "verdict": verdict})
            continue
        raw_drivers = candidate.get("driver_refs")
        rejected.append({
            "candidate_ref": str(candidate.get("debate_ref") or "(unnamed)"),
            "question": str(candidate.get("question") or "")[:400] or "(empty)",
            "driver_refs": sorted({
                str(item) for item in raw_drivers
                if isinstance(item, str) and item.strip()
            }) if isinstance(raw_drivers, list) else [],
            "reasons": verdict["reasons"],
            "observed_at": observed_at,
        })
    return {"admitted": admitted, "rejected": rejected}


# ---------------------------------------------------------------------------
# the closed contract
# ---------------------------------------------------------------------------

_POSITION_FIELDS = {"statement", "claim_refs"}
_MARKET_FIELDS = {"available", "lean", "statement", "refs"}
_OURS_FIELDS = {"state", "side", "statement", "refs"}
_SHIFT_FIELDS = {"reason", "refs"}
_INDEPENDENCE_FIELDS = {"bull_sources", "bear_sources"}
_DEBATE_FIELDS = {
    "debate_ref", "question", "driver_refs", "bull_position", "bear_position",
    "market_position", "our_position", "status", "last_shift_reason",
    "first_seen_at", "source_independence",
}
_REJECTION_FIELDS = {
    "candidate_ref", "question", "driver_refs", "reasons", "observed_at",
}
_ATTRIBUTION_FIELDS = {
    "kind", "work_order_ref", "invocation_ref", "route_decision_ref",
    "model_family",
}
_VERSION_FIELDS = frozenset({
    "schema_version", "id", "created_at", "map_ref", "version",
    "prior_version_ref", "subject_ref", "subject_kind", "change_reason",
    "change_evidence_refs", "constitution_ref", "constitution_hash",
    "policy_ref", "policy_hash", "evidence_fingerprint", "debates",
    "rejected_by_constitution", "drafted_by", "verified_by", "actor_ref",
    "content_hash",
})


def map_ref_for(subject_ref: str) -> str:
    """One chain per subject, named after it."""

    return "debate-map:" + _text(subject_ref, "subject_ref")


def _position(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_POSITION_FIELDS), name)
    wire["statement"] = _text(wire["statement"], f"{name}.statement")
    wire["claim_refs"] = _refs(wire["claim_refs"], f"{name}.claim_refs", nonempty=True)
    return wire


def _market_position(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_MARKET_FIELDS), name)
    if not isinstance(wire["available"], bool):
        raise DebateMapValidationError(f"{name}.available must be a boolean")
    if wire["available"]:
        wire["lean"] = _one_of(wire["lean"], MARKET_LEANS, f"{name}.lean")
        wire["statement"] = _text(wire["statement"], f"{name}.statement")
        wire["refs"] = _refs(wire["refs"], f"{name}.refs", nonempty=True)
    else:
        # "We do not know where the market is" is an answer and has to look
        # different from "the market agrees with us"; an unavailable market
        # position that still carried a lean would read as the second.
        if wire["lean"] is not None or wire["statement"] is not None or wire["refs"]:
            raise DebateMapValidationError(
                f"{name} is unavailable and cannot also state a lean or cite refs"
            )
        wire["refs"] = []
    return wire


def _our_position(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_OURS_FIELDS), name)
    wire["state"] = _one_of(wire["state"], ("held", "none_yet"), f"{name}.state")
    if wire["state"] == "held":
        wire["side"] = _one_of(wire["side"], OUR_SIDES, f"{name}.side")
        wire["statement"] = _text(wire["statement"], f"{name}.statement")
        wire["refs"] = _refs(wire["refs"], f"{name}.refs", nonempty=True)
    else:
        if wire["side"] is not None or wire["statement"] is not None or wire["refs"]:
            raise DebateMapValidationError(
                f"{name} is none_yet and cannot also take a side"
            )
        wire["refs"] = []
    return wire


def _shift(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    wire = _closed(value, set(_SHIFT_FIELDS), name)
    wire["reason"] = _text(wire["reason"], f"{name}.reason")
    wire["refs"] = _refs(wire["refs"], f"{name}.refs", nonempty=True)
    return wire


def _debate(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_DEBATE_FIELDS), name)
    wire["debate_ref"] = _text(wire["debate_ref"], f"{name}.debate_ref")
    wire["question"] = _text(wire["question"], f"{name}.question")
    wire["driver_refs"] = _refs(wire["driver_refs"], f"{name}.driver_refs", nonempty=True)
    wire["bull_position"] = _position(wire["bull_position"], f"{name}.bull_position")
    wire["bear_position"] = _position(wire["bear_position"], f"{name}.bear_position")
    wire["market_position"] = _market_position(
        wire["market_position"], f"{name}.market_position"
    )
    wire["our_position"] = _our_position(wire["our_position"], f"{name}.our_position")
    wire["status"] = _one_of(wire["status"], DEBATE_STATUSES, f"{name}.status")
    wire["last_shift_reason"] = _shift(
        wire["last_shift_reason"], f"{name}.last_shift_reason"
    )
    wire["first_seen_at"] = _text(wire["first_seen_at"], f"{name}.first_seen_at")
    independence = _closed(
        wire["source_independence"], set(_INDEPENDENCE_FIELDS),
        f"{name}.source_independence",
    )
    for field in sorted(_INDEPENDENCE_FIELDS):
        count = independence[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise DebateMapValidationError(
                f"{name}.source_independence.{field} must be a count"
            )
    wire["source_independence"] = independence
    if wire["status"] in ("shifting", "resolved") and wire["last_shift_reason"] is None:
        # A status that says the argument moved has to say what moved it. A
        # resolved debate with no resolving reference is an assertion that
        # nothing else in this repository would accept.
        raise DebateMapValidationError(
            f"{name} is {wire['status']} and must carry a last_shift_reason "
            "naming the evidence that moved it"
        )
    return wire


def _rejection(value: Any, name: str) -> dict[str, Any]:
    wire = _closed(value, set(_REJECTION_FIELDS), name)
    for field in ("candidate_ref", "question", "observed_at"):
        wire[field] = _text(wire[field], f"{name}.{field}")
    wire["driver_refs"] = _refs(wire["driver_refs"], f"{name}.driver_refs")
    reasons = wire["reasons"]
    if not isinstance(reasons, list) or not reasons:
        raise DebateMapValidationError(f"{name}.reasons must be a non-empty array")
    wire["reasons"] = [_one_of(item, GATE_REASONS, f"{name}.reasons[]") for item in reasons]
    if len(set(wire["reasons"])) != len(wire["reasons"]):
        raise DebateMapValidationError(f"{name}.reasons must be unique")
    return wire


def _attribution(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    wire = _closed(value, set(_ATTRIBUTION_FIELDS), name)
    wire["kind"] = _one_of(wire["kind"], ("model", "deterministic"), f"{name}.kind")
    for field in ("work_order_ref", "invocation_ref", "route_decision_ref", "model_family"):
        wire[field] = _optional_text(wire[field], f"{name}.{field}")
    if wire["kind"] == "model" and wire["work_order_ref"] is None:
        raise DebateMapValidationError(f"{name} is a model draft and must name its work order")
    return wire


def cited_refs(version: Mapping[str, Any]) -> set[str]:
    """Every reference a version stands on.

    This is what "new evidence" is measured against: a version that cites
    nothing the last one did not is a rewrite, and ADR-0008 refuses it.
    """

    refs: set[str] = set()
    for debate in version.get("debates") or []:
        for side in ("bull_position", "bear_position"):
            refs.update((debate.get(side) or {}).get("claim_refs") or [])
        refs.update((debate.get("market_position") or {}).get("refs") or [])
        refs.update((debate.get("our_position") or {}).get("refs") or [])
        shift = debate.get("last_shift_reason")
        if shift:
            refs.update(shift.get("refs") or [])
    return refs


def validate_version(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one DebateMapVersion."""

    if not isinstance(value, Mapping):
        raise DebateMapValidationError("DebateMapVersion must be an object")
    wire = dict(value)
    if set(wire) != set(_VERSION_FIELDS):
        raise DebateMapValidationError(
            "DebateMapVersion has invalid closed shape; "
            f"missing={sorted(set(_VERSION_FIELDS) - set(wire))}, "
            f"unknown={sorted(set(wire) - set(_VERSION_FIELDS))}"
        )
    if wire["schema_version"] != SCHEMA_VERSION:
        raise DebateMapValidationError("unsupported DebateMapVersion schema_version")
    for field in ("id", "created_at", "map_ref", "subject_ref", "constitution_ref",
                  "policy_ref", "evidence_fingerprint", "actor_ref"):
        wire[field] = _text(wire[field], field)
    wire["constitution_hash"] = _hash(wire["constitution_hash"], "constitution_hash")
    wire["policy_hash"] = _hash(wire["policy_hash"], "policy_hash")
    wire["subject_kind"] = _one_of(wire["subject_kind"], SUBJECT_KINDS, "subject_kind")
    wire["change_reason"] = _one_of(wire["change_reason"], CHANGE_REASONS, "change_reason")
    wire["change_evidence_refs"] = _refs(
        wire["change_evidence_refs"], "change_evidence_refs", nonempty=True
    )
    if not isinstance(wire["version"], int) or isinstance(wire["version"], bool) \
            or wire["version"] < 1:
        raise DebateMapValidationError("version must be a positive integer")
    wire["prior_version_ref"] = _optional_text(wire["prior_version_ref"], "prior_version_ref")
    if wire["map_ref"] != map_ref_for(wire["subject_ref"]):
        raise DebateMapConflict("map_ref is not derived from the subject")

    debates = wire["debates"]
    if not isinstance(debates, list):
        raise DebateMapValidationError("debates must be an array")
    wire["debates"] = [
        _debate(item, f"debates[{index}]") for index, item in enumerate(debates)
    ]
    if len({item["debate_ref"] for item in wire["debates"]}) != len(wire["debates"]):
        raise DebateMapValidationError("debate_ref must be unique within a version")

    rejected = wire["rejected_by_constitution"]
    if not isinstance(rejected, list):
        raise DebateMapValidationError("rejected_by_constitution must be an array")
    wire["rejected_by_constitution"] = [
        _rejection(item, f"rejected_by_constitution[{index}]")
        for index, item in enumerate(rejected)
    ]

    wire["drafted_by"] = _attribution(wire["drafted_by"], "drafted_by")
    wire["verified_by"] = _attribution(wire["verified_by"], "verified_by")

    # The change reason has to point at evidence this version actually uses.
    # A reason citing a ref that appears nowhere in the map is a reason about
    # some other document.
    unused = set(wire["change_evidence_refs"]) - cited_refs(wire)
    if unused:
        raise DebateMapValidationError(
            "change_evidence_refs name refs this version does not cite: "
            + ", ".join(sorted(unused))
        )

    body = {key: item for key, item in wire.items() if key != "content_hash"}
    expected = content_hash(body)
    if wire["content_hash"] != expected:
        raise DebateMapConflict("DebateMapVersion content hash drifted")
    return wire


def evidence_fingerprint(claim_version_refs: Iterable[str]) -> str:
    """The exact evidence a map was drafted from, as one hash.

    The lane compares this against what the Ledger holds now, which is how
    "this company's evidence changed" is answered without keeping a queue.
    """

    return content_hash({"claim_version_refs": sorted(set(claim_version_refs))})


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------

def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise DebateMapNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise DebateMapConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise DebateMapConflict(f"{name} record_json is not canonical")
    version = validate_version(wire)
    if version["content_hash"] != row["content_hash"] or version["id"] != row["version_id"]:
        raise DebateMapConflict(f"{name} identity columns drifted")
    columns = {
        "map_ref": version["map_ref"],
        "version_number": version["version"],
        "prior_version_id": version["prior_version_ref"],
        "subject_ref": version["subject_ref"],
        "subject_kind": version["subject_kind"],
        "change_reason": version["change_reason"],
        "evidence_fingerprint": version["evidence_fingerprint"],
        "debate_count": len(version["debates"]),
        "live_count": sum(
            1 for item in version["debates"] if item["status"] in LIVE_STATUSES
        ),
        "rejected_count": len(version["rejected_by_constitution"]),
        "actor_ref": version["actor_ref"],
        "created_at": version["created_at"],
    }
    keys = set(row.keys())
    for column, expected in columns.items():
        if column in keys and row[column] != expected:
            raise DebateMapConflict(f"{name} column {column} drifted")
    return version


def table_exists(connection: Any) -> bool:
    """Whether this Core has ever opened a debate map."""

    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


class DebateMapAuthority:
    """Append-only DebateMapVersion chains, one per subject.

    The authority never publishes on its own.  ``publish_map`` is the entry
    point ADR-0008 calls a mechanism: it requires a ``change_reason`` and the
    refs that occasioned it from its caller, and there is no code path in this
    class that calls it.  Deciding *whether* the map should change is the
    judgement layer's job and lives in ``debate_map_draft``.
    """

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("DebateMapAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    # -- reads ------------------------------------------------------------

    def version(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE version_id=?",
            (_text(version_id, "version_id"),),
        ).fetchone()
        return _decode(row, f"DebateMapVersion {version_id}")

    def _latest_row(self, map_ref: str, cursor: Any | None = None) -> sqlite3.Row | None:
        connection = self.connection if cursor is None else cursor
        return connection.execute(
            f"SELECT * FROM {TABLE} WHERE map_ref=? ORDER BY version_number DESC LIMIT 1",
            (map_ref,),
        ).fetchone()

    def current(self, subject_ref: str) -> dict[str, Any] | None:
        """The map as it stands, or ``None`` when the subject has never had one."""

        row = self._latest_row(map_ref_for(subject_ref))
        if row is None:
            return None
        return _decode(row, f"DebateMapVersion for {subject_ref}")

    def versions(self, subject_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE map_ref=? ORDER BY version_number",
            (map_ref_for(subject_ref),),
        ).fetchall()
        return [_decode(row, "DebateMapVersion") for row in rows]

    def subjects(self) -> list[str]:
        return [
            row["subject_ref"] for row in self.connection.execute(
                f"SELECT DISTINCT subject_ref FROM {TABLE} ORDER BY subject_ref"
            ).fetchall()
        ]

    def counts(self) -> dict[str, int]:
        maps = self.connection.execute(
            f"SELECT COUNT(DISTINCT map_ref) FROM {TABLE}"
        ).fetchone()[0]
        versions = self.connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        return {"maps": int(maps), "versions": int(versions)}

    # -- the reads the brief and the judgement prompts make ---------------

    def open_debates(self, subject_ref: str) -> list[dict[str, Any]]:
        """What is still being argued about for one subject.

        ``open`` and ``shifting`` both, because a debate that is moving is
        still open -- ``shifting`` says which way it is going, not that it is
        over.  ``candidate`` is excluded: it is a question this system has not
        yet found two independent voices on, and putting it in the weekly
        brief beside a real disagreement would misprice both.
        """

        current = self.current(subject_ref)
        if current is None:
            return []
        return [
            dict(debate) for debate in current["debates"]
            if debate["status"] in LIVE_STATUSES
        ]

    def shifted_since(self, subject_ref: str, version: int | str) -> dict[str, Any]:
        """What changed between a version the reader has seen and now.

        ``version`` is a version number or a version id, because the weekly
        brief remembers the id it quoted and a human remembers the number.
        """

        chain = self.versions(subject_ref)
        if not chain:
            raise DebateMapNotFound(f"no debate map for {subject_ref}")
        if isinstance(version, int) and not isinstance(version, bool):
            base = next((item for item in chain if item["version"] == version), None)
        else:
            base = next((item for item in chain if item["id"] == version), None)
        if base is None:
            raise DebateMapNotFound(f"debate map version {version!r} is not in this chain")
        latest = chain[-1]
        before = {item["debate_ref"]: item for item in base["debates"]}
        changes: list[dict[str, Any]] = []
        for debate in latest["debates"]:
            prior = before.get(debate["debate_ref"])
            prior_refs = set()
            if prior is not None:
                prior_refs = cited_refs({"debates": [prior]})
            new_refs = sorted(cited_refs({"debates": [debate]}) - prior_refs)
            if prior is not None and prior["status"] == debate["status"] and not new_refs:
                continue
            changes.append({
                "debate_ref": debate["debate_ref"],
                "question": debate["question"],
                "driver_refs": list(debate["driver_refs"]),
                "from_status": None if prior is None else prior["status"],
                "to_status": debate["status"],
                "last_shift_reason": debate["last_shift_reason"],
                "new_refs": new_refs,
            })
        gone = [
            {"debate_ref": ref, "question": item["question"],
             "from_status": item["status"], "to_status": None,
             "driver_refs": list(item["driver_refs"]),
             "last_shift_reason": None, "new_refs": []}
            for ref, item in before.items()
            if ref not in {debate["debate_ref"] for debate in latest["debates"]}
        ]
        return {
            "subject_ref": subject_ref,
            "from_version": base["version"],
            "from_version_ref": base["id"],
            "to_version": latest["version"],
            "to_version_ref": latest["id"],
            "changed": changes,
            "dropped": sorted(gone, key=lambda item: item["debate_ref"]),
        }

    def debate_for_driver(self, driver_ref: str) -> list[dict[str, Any]]:
        """Every current debate bound to one driver, across all subjects.

        The forecast layer's question, not the brief's: before an assumption
        is revised, what is the argument about the driver it hangs on.
        """

        driver_ref = _text(driver_ref, "driver_ref")
        found: list[dict[str, Any]] = []
        for subject in self.subjects():
            current = self.current(subject)
            if current is None:
                continue
            for debate in current["debates"]:
                if driver_ref in debate["driver_refs"]:
                    found.append({
                        "subject_ref": subject,
                        "version_ref": current["id"],
                        "version": current["version"],
                        **dict(debate),
                    })
        return found

    # -- the write --------------------------------------------------------

    def publish_map(
        self,
        *,
        subject_ref: str,
        subject_kind: str,
        change_reason: str,
        change_evidence_refs: Sequence[str],
        constitution_ref: str,
        constitution_hash: str,
        evidence_fingerprint: str,
        debates: Sequence[Mapping[str, Any]],
        rejected_by_constitution: Sequence[Mapping[str, Any]] = (),
        drafted_by: Mapping[str, Any] | None = None,
        verified_by: Mapping[str, Any] | None = None,
        actor_ref: str,
        created_at: str,
        policy_ref: str = POLICY_REF,
        policy_hash: str = POLICY_HASH,
    ) -> dict[str, Any]:
        """Publish one version, or refuse it as a ``duplicate``.

        A new version exists only when it can say what it learned: a debate
        that is new, a status that moved, or a reference the current version
        does not cite.  Anything else is the same map written twice.
        """

        subject_ref = _text(subject_ref, "subject_ref")
        map_ref = map_ref_for(subject_ref)
        body = {
            "map_ref": map_ref,
            "subject_ref": subject_ref,
            "subject_kind": _one_of(subject_kind, SUBJECT_KINDS, "subject_kind"),
            "change_reason": _one_of(change_reason, CHANGE_REASONS, "change_reason"),
            "change_evidence_refs": _refs(
                list(change_evidence_refs), "change_evidence_refs", nonempty=True
            ),
            "constitution_ref": _text(constitution_ref, "constitution_ref"),
            "constitution_hash": _hash(constitution_hash, "constitution_hash"),
            "policy_ref": _text(policy_ref, "policy_ref"),
            "policy_hash": _hash(policy_hash, "policy_hash"),
            "evidence_fingerprint": _text(evidence_fingerprint, "evidence_fingerprint"),
            "debates": [dict(item) for item in debates],
            "rejected_by_constitution": [dict(item) for item in rejected_by_constitution],
            "drafted_by": None if drafted_by is None else dict(drafted_by),
            "verified_by": None if verified_by is None else dict(verified_by),
            "actor_ref": _text(actor_ref, "actor_ref"),
        }
        created_at = _text(created_at, "created_at")

        latest_row = self._latest_row(map_ref)
        latest = None if latest_row is None else _decode(
            latest_row, f"DebateMapVersion for {subject_ref}"
        )
        candidate_wire = self._compose(
            body,
            version=1 if latest is None else latest["version"] + 1,
            prior_version_ref=None if latest is None else latest["id"],
            created_at=created_at,
        )
        decision = novelty(latest, candidate_wire)
        if not decision["new"]:
            return {"status": "duplicate", "reason": decision["reason"], **latest}

        with self.store._transaction() as cur:
            cur.execute(
                f"INSERT INTO {TABLE}(version_id,map_ref,version_number,prior_version_id,"
                "subject_ref,subject_kind,change_reason,evidence_fingerprint,debate_count,"
                "live_count,rejected_count,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate_wire["id"], candidate_wire["map_ref"],
                    candidate_wire["version"], candidate_wire["prior_version_ref"],
                    candidate_wire["subject_ref"], candidate_wire["subject_kind"],
                    candidate_wire["change_reason"], candidate_wire["evidence_fingerprint"],
                    len(candidate_wire["debates"]),
                    sum(1 for item in candidate_wire["debates"]
                        if item["status"] in LIVE_STATUSES),
                    len(candidate_wire["rejected_by_constitution"]),
                    canonical_json(candidate_wire), candidate_wire["content_hash"],
                    candidate_wire["actor_ref"], candidate_wire["created_at"],
                ),
            )
        stored = self.version(candidate_wire["id"])
        if stored["content_hash"] != candidate_wire["content_hash"]:
            raise DebateMapConflict("stored DebateMapVersion did not read back")
        return {"status": "fresh", "reason": decision["reason"], **stored}

    def _compose(
        self,
        body: Mapping[str, Any],
        *,
        version: int,
        prior_version_ref: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        identity = {"version": version, "prior_version_ref": prior_version_ref, **dict(body)}
        wire = {
            "schema_version": SCHEMA_VERSION,
            "id": "debate-map-version:" + content_hash(identity)[:32],
            "created_at": created_at,
            "version": version,
            "prior_version_ref": prior_version_ref,
            **dict(body),
        }
        wire["content_hash"] = content_hash(wire)
        return validate_version(wire)


def novelty(
    prior: Mapping[str, Any] | None, candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """ADR-0008's test, as one function so both sides read the same rule."""

    if prior is None:
        return {"new": True, "reason": "first_version"}
    prior_refs = {item["debate_ref"] for item in prior["debates"]}
    fresh_debates = sorted(
        {item["debate_ref"] for item in candidate["debates"]} - prior_refs
    )
    if fresh_debates:
        return {"new": True, "reason": "new_debate:" + fresh_debates[0]}
    prior_status = {item["debate_ref"]: item["status"] for item in prior["debates"]}
    for debate in candidate["debates"]:
        if prior_status.get(debate["debate_ref"]) != debate["status"]:
            return {"new": True, "reason": "status_change:" + debate["debate_ref"]}
    new_refs = sorted(cited_refs(candidate) - cited_refs(prior))
    if new_refs:
        return {"new": True, "reason": "new_ref:" + new_refs[0]}
    return {
        "new": False,
        "reason": "no new debate, no status change and no reference the "
                  "current version does not already cite",
    }


__all__ = [
    "CANDIDATE_FIELDS",
    "CHANGE_REASONS",
    "DEBATE_POLICY",
    "DEBATE_STATUSES",
    "GAINING",
    "GATE_REASONS",
    "LIVE_STATUSES",
    "MARKET_LEANS",
    "OUR_SIDES",
    "POLICY_HASH",
    "POLICY_REF",
    "SCHEMA_VERSION",
    "SOURCE_BASES",
    "SUBJECT_KINDS",
    "TABLE",
    "DebateMapAuthority",
    "DebateMapConflict",
    "DebateMapError",
    "DebateMapNotFound",
    "DebateMapValidationError",
    "cited_refs",
    "contested_aspects",
    "count_independent_sources",
    "evidence_fingerprint",
    "index_claims",
    "map_ref_for",
    "novelty",
    "polarity",
    "publisher_of",
    "screen_candidate",
    "screen_candidates",
    "source_identity",
    "table_exists",
    "tier_of",
    "validate_version",
]
