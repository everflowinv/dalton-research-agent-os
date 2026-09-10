"""P12c drafting: one bounded call finds the arguments, a second one checks them.

The input is a table, not a request to be creative.  A drafting run sees the
company's canonical Claims grouped by aspect and labelled with their source
tier, the drivers it may bind to, the thesis we hold, the numbered admission
rules and causal-chain links from the active constitution, and the previous
map.  It may cite nothing else.  A reply that names a row it was not shown is
not partly right: it is evidence the reply was not produced from the table, so
the whole draft is refused and none of it is kept.

Two things the prompt insists on, because they are the only reason the object
exists:

*Where the market is, separately from where we are.*  The owner's rule is that
agreeing with the market has no value.  A debate that does not distinguish the
two is a summary of what has been said, and the version contract refuses it.
When consensus genuinely has not spoken, the honest answer is
``available: false`` -- which is a finding, not a gap.

*Which side has been gaining since the previous version.*  That is what turns
``open`` into ``shifting``, and it is a question only something holding both
versions can answer, which is why the previous map is in the prompt.

The verifier is a second call to a *different model family* that returns a
verdict and nothing else.  It fails closed: when the family of either call
cannot be established, the draft is not published.  A verification that cannot
prove it was independent is not a verification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import CockpitModelError, register_purpose, unwrap_json_object
from .debate_map import (
    CHANGE_REASONS,
    DEBATE_POLICY,
    POLICY_HASH,
    POLICY_REF,
    contested_aspects,
    index_claims,
    screen_candidates,
    tier_of,
)
from .driver_template import prompt_block
from .store import content_hash

SCHEMA_VERSION = "0.1"
TASK_REF = "task:debate-map-draft:0.1"

# The lane names its own purpose from its own module rather than editing a set
# in cockpit_model (P14-0's registry).
PURPOSE = register_purpose("debate_map")

# One company, one call.  The bounds are on spend, not ambition: the router
# reserves against prompt bytes, so a table that doubles doubles the
# reservation whether or not it doubles the price.
MAX_CLAIM_ROWS = 60
MAX_STATEMENT_CHARS = 320
MAX_PREVIOUS_DEBATES = 12
# How many debates one draft may carry.  A map is a short list by nature -- the
# blueprint's acceptance bar is three for Accenture -- and a reply with forty is
# not a thorough analyst, it is a model that stopped choosing.  Bounded here
# rather than trimmed, because trimming would keep whichever ones happened to
# come first out of a reply that was already not answering the question.
MAX_DEBATES = 12
MAX_PROMPT_BYTES = 48_000
MAX_COST_USD = 0.80
MAX_INPUT_TOKENS = 80_000
MAX_OUTPUT_TOKENS = 4_000
TIMEOUT_SECONDS = 240

MAX_QUESTION_CHARS = 300
MAX_STATEMENT_OUT_CHARS = 600

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "position_not_supported_by_cited_claims",
    "sides_are_not_actually_opposed",
    "market_position_misstated",
    "our_position_not_in_the_thesis",
    "question_not_on_the_named_causal_link",
    "shift_not_supported_by_refs",
)

_CANDIDATE_KEYS = frozenset({
    "debate_ref", "question", "driver_refs", "question_admission_index",
    "causal_chain_index", "bull", "bear", "market", "ours", "gaining",
    "resolution",
})
_SIDE_KEYS = frozenset({"statement", "claim_refs"})
_MARKET_KEYS = frozenset({"available", "lean", "statement", "refs"})
_OURS_KEYS = frozenset({"state", "side", "statement", "refs"})
_RESOLUTION_KEYS = frozenset({"reason", "refs"})

# Versioned separately from the DebateMap authority.  This is the exact model
# output boundary; changing it must release a persisted lane hold and create a
# new WorkOrder identity without pretending the underlying evidence changed.
DRAFT_CONTRACT_VERSION = "debate-map-draft-output-0.3"
DRAFT_CONTRACT_HASH = content_hash({
    "version": DRAFT_CONTRACT_VERSION,
    "top_level": ["debates"],
    "candidate": sorted(_CANDIDATE_KEYS),
    "side": sorted(_SIDE_KEYS),
    "market": sorted(_MARKET_KEYS),
    "ours": sorted(_OURS_KEYS),
    "resolution": sorted(_RESOLUTION_KEYS),
    "limits": {
        "debates": MAX_DEBATES,
        "question_chars": MAX_QUESTION_CHARS,
        "statement_chars": MAX_STATEMENT_OUT_CHARS,
    },
})

TASK_HASH = content_hash({
    "task": TASK_REF,
    "policy_ref": POLICY_REF,
    "policy_hash": POLICY_HASH,
    "output": "one JSON object with a debates array in the closed draft shape",
    "authority": "cites_only_shown_row_ids_and_only_shown_debate_refs",
    "draft_contract_version": DRAFT_CONTRACT_VERSION,
    "draft_contract_hash": DRAFT_CONTRACT_HASH,
})


class DebateDraftError(ValueError):
    """A draft cannot be produced or cannot be trusted."""


class DebateDraftRefused(DebateDraftError):
    """A reply went outside what it was shown; the whole draft is refused."""


# ---------------------------------------------------------------------------
# the input table
# ---------------------------------------------------------------------------

def build_input_table(
    *,
    subject_ref: str,
    subject_kind: str,
    claim_rows: Sequence[Mapping[str, Any]],
    driver_rows: Sequence[Mapping[str, Any]],
    thesis: Mapping[str, Any] | None,
    method: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    policy: Mapping[str, Any] = DEBATE_POLICY,
    max_claim_rows: int = MAX_CLAIM_ROWS,
    max_statement_chars: int = MAX_STATEMENT_CHARS,
    industry_classification: Any = None,
) -> dict[str, Any]:
    """Everything one drafting call may look at, and the ids it may cite.

    Claims are ordered by the deterministic pre-pass first: an aspect the
    evidence already disagrees about is where a debate is, and putting those
    rows at the top of a bounded table is the difference between a call that
    finds three arguments and one that summarises a quarter.
    """

    indexed = index_claims(claim_rows, policy)
    contested = contested_aspects(claim_rows, policy)
    priority = {row["aspect"]: index for index, row in enumerate(contested)}

    def order(item: tuple[str, Mapping[str, Any]]) -> tuple[Any, ...]:
        ref, row = item
        aspect = str(row.get("index_aspect") or row.get("aspect") or "")
        tiers = list(policy["source_tiers"])
        tier = tier_of(row, policy)
        return (
            priority.get(aspect, len(contested)),
            aspect,
            tiers.index(tier) if tier in tiers else len(tiers),
            ref,
        )

    rows: list[dict[str, Any]] = []
    for ref, row in sorted(indexed.items(), key=order):
        if len(rows) >= max_claim_rows:
            break
        statement = str(row.get("normalized_statement") or "").strip()
        if row.get("value") is not None:
            statement = f"{row['value']} {row.get('unit') or ''} — {statement}".strip()
        rows.append({
            "row_id": f"C{len(rows) + 1}",
            "claim_version_ref": ref,
            "aspect": str(row.get("index_aspect") or row.get("aspect") or "other"),
            "tier": tier_of(row, policy),
            "publisher": row.get("publisher") or "unattributed",
            "as_of": row.get("as_of") or "",
            "statement": statement[:max_statement_chars],
        })

    thesis_rows: list[dict[str, Any]] = []
    if thesis and thesis.get("status") == "current" and thesis.get("thesis_version_ref"):
        thesis_rows.append({
            "row_id": "T1",
            "ref": thesis["thesis_version_ref"],
            "statement": str(thesis.get("statement") or "").strip()[:max_statement_chars],
            "confidence": thesis.get("confidence"),
            "driver_refs": list(thesis.get("driver_refs") or []),
        })

    previous_debates: list[dict[str, Any]] = []
    for debate in (previous or {}).get("debates", [])[:MAX_PREVIOUS_DEBATES]:
        previous_debates.append({
            "debate_ref": debate["debate_ref"],
            "question": debate["question"],
            "status": debate["status"],
            "driver_refs": list(debate["driver_refs"]),
            "first_seen_at": debate["first_seen_at"],
        })

    citable = {row["row_id"]: row["claim_version_ref"] for row in rows}
    citable.update({row["row_id"]: row["ref"] for row in thesis_rows})
    return {
        "subject_ref": subject_ref,
        "subject_kind": subject_kind,
        "claims": rows,
        "claim_index": indexed,
        "contested": contested,
        "drivers": [
            {"driver_ref": str(row["driver_ref"]), "label": str(row.get("label") or ""),
             "mechanism": str(row.get("mechanism") or "")}
            for row in driver_rows
        ],
        "thesis_rows": thesis_rows,
        "question_admission": list(method.get("question_admission") or []),
        "causal_chain": list(method.get("causal_chain") or []),
        "previous_debates": previous_debates,
        "citable": citable,
        # W4: which driver questions this *kind* of company is argued about,
        # shown to the drafter beside the drivers it may bind to. Not a gate --
        # the constitution gate is the gate, and it checks driver refs, not
        # subject matter -- but a map of a commodity producer with nothing on
        # it about the spread has a hole in it, and the drafter is the cheapest
        # place to notice.
        "industry_classification": (
            None if industry_classification is None
            else str(industry_classification)),
        "driver_template": prompt_block(industry_classification),
        "policy_ref": POLICY_REF,
        "policy_hash": POLICY_HASH,
    }


def _numbered(items: Sequence[str]) -> str:
    return "\n".join(f"  [{index}] {item}" for index, item in enumerate(items)) or "  (none)"


def build_prompt(table: Mapping[str, Any]) -> str:
    """The drafting prompt: five tables and one closed answer shape."""

    claims = "\n".join(
        "\t".join((
            row["row_id"], row["aspect"], row["tier"], row["publisher"],
            row["as_of"], row["statement"].replace("\t", " "),
        ))
        for row in table["claims"]
    ) or "  (no claims)"
    drivers = "\n".join(
        f"  {row['driver_ref']}\t{row['label']}\t{row['mechanism']}"
        for row in table["drivers"]
    ) or "  (none)"
    thesis = "\n".join(
        f"  {row['row_id']}\t{row['statement']} (confidence {row['confidence']})"
        for row in table["thesis_rows"]
    ) or "  (we hold no admitted thesis on this subject)"
    previous = "\n".join(
        f"  {row['debate_ref']}\t{row['status']}\t{row['question']}"
        for row in table["previous_debates"]
    ) or "  (no previous map)"
    return (
        "You keep the map of what is contested about one research subject.\n"
        "You are not writing a summary. A debate that both sides of the market "
        "agree about is worth nothing; what is worth writing down is where we "
        "differ from consensus and what evidence would settle it.\n\n"
        f"SUBJECT: {table['subject_ref']} ({table['subject_kind']})\n\n"
        "DRIVERS you may bind a debate to -- <driver_ref>\\t<label>\\t<mechanism>:\n"
        f"{drivers}\n\n"
        "QUESTION ADMISSION rules from the active research constitution. A "
        "debate must be admitted by exactly one of them; name its number:\n"
        f"{_numbered(table['question_admission'])}\n\n"
        "CAUSAL CHAIN under study. A debate must sit on exactly one link; "
        "name its number:\n"
        f"{_numbered(table['causal_chain'])}\n\n"
        "OUR THESIS -- <row id>\\t<statement>:\n"
        f"{thesis}\n\n"
        "PREVIOUS DEBATES -- <debate_ref>\\t<status>\\t<question>:\n"
        f"{previous}\n\n"
        f"{table.get('driver_template') or ''}\n\n"
        "CLAIMS, one per row, tab separated. The tier is how much the source "
        "is worth and the publisher is who said it:\n"
        "  <row id>\\t<aspect>\\t<tier>\\t<publisher>\\t<as of>\\t<statement>\n"
        f"{claims}\n\n"
        f"OUTPUT CONTRACT: {DRAFT_CONTRACT_VERSION} ({DRAFT_CONTRACT_HASH}).\n"
        "Rules:\n"
        "* Cite only the row ids above (C..., T...). Never invent one.\n"
        "* Every debate binds at least one driver_ref copied exactly from "
        "DRIVERS.\n"
        "* Both sides need evidence. A side with no claim behind it is not a "
        "side, and a debate you cannot evidence both ways is not a debate.\n"
        "* market_position is where consensus stands -- sell-side ratings and "
        "targets, the sales-note and crowd tiers, management's own framing. "
        "If the rows do not tell you, say available:false rather than "
        "guessing; that is an answer.\n"
        "* our_position is what OUR THESIS commits us to. If we have no view, "
        "say none_yet. Do not copy the market into it.\n"
        "* gaining says which side has been gaining ground since PREVIOUS "
        "DEBATES; use 'neither' when nothing moved or there is no previous "
        "map.\n"
        "* If a debate below continues one in PREVIOUS DEBATES, reuse that "
        "debate_ref exactly. Otherwise use a new id of the form new-1, new-2.\n"
        "* resolution is null unless the argument is settled, in which case "
        "it names the rows that settled it.\n"
        "* Return one raw JSON object and nothing else. No prose, no code "
        "fence. The only top-level key is debates. Do not return "
        "template_coverage, evidence_limits, commentary, or any other sibling "
        "key; those fields make the entire answer unusable.\n"
        f"* Return at most {MAX_DEBATES} debates. Every question is at most "
        f"{MAX_QUESTION_CHARS} characters. Every bull.statement, "
        "bear.statement, market.statement, ours.statement, and "
        f"resolution.reason is at most {MAX_STATEMENT_OUT_CHARS} characters. "
        "These character limits include spaces.\n\n"
        "{\"debates\": [{\"debate_ref\": \"new-1\", \"question\": \"...\",\n"
        "  \"driver_refs\": [\"<driver_ref>\"], \"question_admission_index\": 0,\n"
        "  \"causal_chain_index\": 0,\n"
        "  \"bull\": {\"statement\": \"...\", \"claim_refs\": [\"C1\"]},\n"
        "  \"bear\": {\"statement\": \"...\", \"claim_refs\": [\"C2\"]},\n"
        "  \"market\": {\"available\": true, \"lean\": \"bull|bear|split\",\n"
        "     \"statement\": \"...\", \"refs\": [\"C1\"]},\n"
        "  \"ours\": {\"state\": \"held|none_yet\", \"side\": \"bull|bear|neither\",\n"
        "     \"statement\": \"...\", \"refs\": [\"T1\"]},\n"
        "  \"gaining\": \"bull|bear|neither\", \"resolution\": null}]}\n"
    )


def prompt_drafter(work_order_ref: str, prompt: str) -> tuple[str, str]:
    """The drafter ref and hash a debate map version is recorded under."""

    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return (
        f"model:{work_order_ref}",
        content_hash({"task_hash": TASK_HASH, "prompt_sha256": digest}),
    )


# ---------------------------------------------------------------------------
# parsing the draft
# ---------------------------------------------------------------------------

def _text_field(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DebateDraftRefused(f"{name} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise DebateDraftRefused(f"{name} is longer than {limit} characters")
    return text


def _shown_refs(value: Any, name: str, citable: Mapping[str, str], *, nonempty: bool) -> list[str]:
    if not isinstance(value, list):
        raise DebateDraftRefused(f"{name} must be an array of row ids")
    resolved: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in citable:
            raise DebateDraftRefused(
                f"{name} names {item!r}, which was not a row in the table"
            )
        ref = citable[item]
        if ref not in resolved:
            resolved.append(ref)
    if nonempty and not resolved:
        raise DebateDraftRefused(f"{name} must name at least one row")
    return resolved


def debate_ref_for(subject_ref: str, driver_refs: Sequence[str], question: str) -> str:
    """A new debate's stable name.

    Content addressed on the subject, the drivers and the folded question so
    that two runs proposing the same argument agree without co-ordinating.  A
    debate that continues an existing one keeps the old ref instead, which is
    what makes the chain readable as one argument moving rather than a new
    argument every week.
    """

    from .claim_index_tagging import fold

    return "debate:" + content_hash({
        "subject_ref": subject_ref,
        "driver_refs": sorted(driver_refs),
        "question": fold(question),
    })[:24]


def parse_draft(text: Any, table: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The reply, or a refusal of the whole draft.

    Verified against the table rather than merely parsed.  The closed shape is
    checked key by key: an unknown key means the reply was produced against a
    different contract, and a reply produced against a different contract
    cannot be trusted where it happens to agree with this one.
    """

    if not isinstance(text, str) or not text.strip():
        raise DebateDraftRefused("the drafter returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None:
        raise DebateDraftRefused("the drafter did not return one JSON object")
    if set(parsed) != {"debates"}:
        raise DebateDraftRefused(
            "the draft must be exactly {\"debates\": [...]}; got keys "
            f"{sorted(parsed)}"
        )
    rows = parsed["debates"]
    if not isinstance(rows, list) or not rows:
        raise DebateDraftRefused("the draft carries no debates")
    if len(rows) > MAX_DEBATES:
        raise DebateDraftRefused(
            f"the draft carries {len(rows)} debates, over the {MAX_DEBATES} bound"
        )
    citable = dict(table["citable"])
    known_drivers = {row["driver_ref"] for row in table["drivers"]}
    previous_refs = {row["debate_ref"] for row in table["previous_debates"]}
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        name = f"debates[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != _CANDIDATE_KEYS:
            raise DebateDraftRefused(
                f"{name} has an invalid closed shape; expected exactly "
                f"{sorted(_CANDIDATE_KEYS)}"
            )
        question = _text_field(raw["question"], f"{name}.question", MAX_QUESTION_CHARS)
        drivers = raw["driver_refs"]
        if not isinstance(drivers, list) or not drivers:
            raise DebateDraftRefused(f"{name}.driver_refs must name a driver")
        for driver in drivers:
            if not isinstance(driver, str) or driver not in known_drivers:
                raise DebateDraftRefused(
                    f"{name}.driver_refs names {driver!r}, which is not in DRIVERS"
                )
        drivers = sorted(dict.fromkeys(str(item) for item in drivers))

        sides: dict[str, dict[str, Any]] = {}
        for side in ("bull", "bear"):
            block = raw[side]
            if not isinstance(block, Mapping) or set(block) != _SIDE_KEYS:
                raise DebateDraftRefused(f"{name}.{side} has an invalid closed shape")
            sides[side] = {
                "statement": _text_field(
                    block["statement"], f"{name}.{side}.statement", MAX_STATEMENT_OUT_CHARS
                ),
                "claim_refs": _shown_refs(
                    block["claim_refs"], f"{name}.{side}.claim_refs", citable, nonempty=True
                ),
            }

        market = raw["market"]
        if not isinstance(market, Mapping) or set(market) != _MARKET_KEYS:
            raise DebateDraftRefused(f"{name}.market has an invalid closed shape")
        if not isinstance(market["available"], bool):
            raise DebateDraftRefused(f"{name}.market.available must be true or false")
        if market["available"]:
            if market["lean"] not in ("bull", "bear", "split"):
                raise DebateDraftRefused(f"{name}.market.lean is not a lean")
            market_wire = {
                "available": True,
                "lean": str(market["lean"]),
                "statement": _text_field(
                    market["statement"], f"{name}.market.statement", MAX_STATEMENT_OUT_CHARS
                ),
                "refs": _shown_refs(
                    market["refs"], f"{name}.market.refs", citable, nonempty=True
                ),
            }
        else:
            market_wire = {"available": False, "lean": None, "statement": None, "refs": []}

        ours = raw["ours"]
        if not isinstance(ours, Mapping) or set(ours) != _OURS_KEYS:
            raise DebateDraftRefused(f"{name}.ours has an invalid closed shape")
        if ours["state"] not in ("held", "none_yet"):
            raise DebateDraftRefused(f"{name}.ours.state must be held or none_yet")
        if ours["state"] == "held":
            if ours["side"] not in ("bull", "bear", "neither"):
                raise DebateDraftRefused(f"{name}.ours.side is not a side")
            ours_wire = {
                "state": "held",
                "side": str(ours["side"]),
                "statement": _text_field(
                    ours["statement"], f"{name}.ours.statement", MAX_STATEMENT_OUT_CHARS
                ),
                "refs": _shown_refs(ours["refs"], f"{name}.ours.refs", citable, nonempty=True),
            }
        else:
            ours_wire = {"state": "none_yet", "side": None, "statement": None, "refs": []}

        if raw["gaining"] not in ("bull", "bear", "neither"):
            raise DebateDraftRefused(f"{name}.gaining must be bull, bear or neither")

        resolution = raw["resolution"]
        if resolution is None:
            resolution_wire = None
        elif isinstance(resolution, Mapping) and set(resolution) == _RESOLUTION_KEYS:
            resolution_wire = {
                "reason": _text_field(
                    resolution["reason"], f"{name}.resolution.reason", MAX_STATEMENT_OUT_CHARS
                ),
                "refs": _shown_refs(
                    resolution["refs"], f"{name}.resolution.refs", citable, nonempty=True
                ),
            }
        else:
            raise DebateDraftRefused(f"{name}.resolution has an invalid closed shape")

        raw_ref = raw["debate_ref"]
        if not isinstance(raw_ref, str) or not raw_ref.strip():
            raise DebateDraftRefused(f"{name}.debate_ref must be text")
        raw_ref = raw_ref.strip()
        if raw_ref in previous_refs:
            debate_ref = raw_ref
        elif raw_ref.startswith("new-") and raw_ref[4:].isdigit():
            debate_ref = debate_ref_for(table["subject_ref"], drivers, question)
        else:
            raise DebateDraftRefused(
                f"{name}.debate_ref {raw_ref!r} is neither a previous debate nor "
                "a new-<n> id"
            )
        if debate_ref in seen:
            raise DebateDraftRefused(f"{name} repeats debate {debate_ref}")
        seen.add(debate_ref)

        for field in ("question_admission_index", "causal_chain_index"):
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise DebateDraftRefused(f"{name}.{field} must be a row number")

        candidates.append({
            "debate_ref": debate_ref,
            "question": question,
            "driver_refs": drivers,
            "question_admission_index": raw["question_admission_index"],
            "causal_chain_index": raw["causal_chain_index"],
            "bull": sides["bull"],
            "bear": sides["bear"],
            "market": market_wire,
            "ours": ours_wire,
            "gaining": str(raw["gaining"]),
            "resolution": resolution_wire,
        })
    return candidates


# ---------------------------------------------------------------------------
# assembling the version's debates
# ---------------------------------------------------------------------------

def assemble_debates(
    screened: Sequence[Mapping[str, Any]],
    *,
    previous: Mapping[str, Any] | None,
    created_at: str,
) -> list[dict[str, Any]]:
    """Turn admitted candidates into the authority's closed debate shape."""

    before = {
        item["debate_ref"]: item for item in (previous or {}).get("debates", [])
    }
    debates: list[dict[str, Any]] = []
    for entry in screened:
        candidate = entry["candidate"]
        verdict = entry["verdict"]
        ref = candidate["debate_ref"]
        prior = before.get(ref)
        status = verdict["status"]
        if status == "resolved":
            shift = dict(candidate["resolution"])
        elif status == "shifting":
            shift = {
                "reason": f"{candidate['gaining']} side gaining: "
                          + candidate[candidate["gaining"]]["statement"],
                "refs": list(candidate[candidate["gaining"]]["claim_refs"]),
            }
        elif prior is not None and prior["status"] in ("shifting", "resolved") \
                and prior["last_shift_reason"] is not None:
            # A debate that moved and then settled back to open keeps the note
            # of what moved it; dropping it would make the chain unreadable at
            # exactly the version a reader cares about.
            shift = dict(prior["last_shift_reason"])
        else:
            shift = None
        debates.append({
            "debate_ref": ref,
            "question": candidate["question"],
            "driver_refs": list(candidate["driver_refs"]),
            "admission_index": candidate["question_admission_index"],
            "causal_link_index": candidate["causal_chain_index"],
            "bull_position": dict(candidate["bull"]),
            "bear_position": dict(candidate["bear"]),
            "market_position": dict(candidate["market"]),
            "our_position": dict(candidate["ours"]),
            "status": status,
            "last_shift_reason": shift,
            "first_seen_at": created_at if prior is None else prior["first_seen_at"],
            "source_independence": {
                "bull_sources": verdict["source_independence"]["bull_sources"],
                "bear_sources": verdict["source_independence"]["bear_sources"],
            },
        })
    debates.sort(key=lambda item: item["debate_ref"])
    return debates


# ---------------------------------------------------------------------------
# the verifier
# ---------------------------------------------------------------------------

def build_verifier_prompt(
    table: Mapping[str, Any], debates: Sequence[Mapping[str, Any]]
) -> str:
    """The verifier sees the same table and answers one question."""

    by_ref = {ref: row_id for row_id, ref in table["citable"].items()}
    claims = "\n".join(
        f"  {row['row_id']}\t{row['tier']}\t{row['publisher']}\t{row['statement']}"
        for row in table["claims"]
    ) or "  (no claims)"
    lines = [
        "You are an independent verifier. Another model drew up a map of what is",
        "contested about one research subject from the table below. You do not",
        "redraw it and you do not improve it. You answer two questions: is each",
        "debate supported by the rows it cites, and does it sit on the causal",
        "link and satisfy the admission rule it named?",
        "",
        f"SUBJECT: {table['subject_ref']}",
        "",
        "QUESTION ADMISSION rules:",
        _numbered(table["question_admission"]),
        "",
        "CAUSAL CHAIN:",
        _numbered(table["causal_chain"]),
        "",
        "CLAIMS -- <row id>\\t<tier>\\t<publisher>\\t<statement>:",
        claims,
        "",
        "DEBATES under review:",
    ]
    for debate in debates:
        lines.append(f"- {debate['debate_ref']} [{debate['status']}] {debate['question']}")
        lines.append(f"    drivers: {', '.join(debate['driver_refs'])}")
        # The placement the drafter asserted, as the numbers it named. The gate
        # could only check the numbers exist; whether the question really sits
        # there is a reading, and this is the reader.
        lines.append(
            f"    admission [{debate['admission_index']}] / "
            f"link [{debate['causal_link_index']}]"
        )
        for side, key in (("bull", "bull_position"), ("bear", "bear_position")):
            cited = " ".join(by_ref.get(ref, ref) for ref in debate[key]["claim_refs"])
            lines.append(f"    {side}: {debate[key]['statement']}  [{cited}]")
        market = debate["market_position"]
        if market["available"]:
            cited = " ".join(by_ref.get(ref, ref) for ref in market["refs"])
            lines.append(f"    market ({market['lean']}): {market['statement']}  [{cited}]")
        else:
            lines.append("    market: not established from these rows")
        ours = debate["our_position"]
        if ours["state"] == "held":
            lines.append(f"    ours ({ours['side']}): {ours['statement']}")
        else:
            lines.append("    ours: none yet")
        shift = debate["last_shift_reason"]
        if shift is not None:
            cited = " ".join(by_ref.get(ref, ref) for ref in shift["refs"])
            lines.append(f"    shift: {shift['reason']}  [{cited}]")
    lines += [
        "",
        "Return one raw JSON object and nothing else:",
        '{"verdict": "pass|reject", "findings": [{"debate_ref": "<ref>",',
        '   "code": "' + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict has no findings; a reject verdict has at least one.",
        "Use question_not_on_the_named_causal_link when a debate's admission",
        "rule or causal link does not actually cover the question it asks.",
    ]
    prompt = "\n".join(lines)
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        # The verifier's prompt carries the drafter's claims table plus every
        # debate written out, so it is the larger of the two and the one that
        # can cross the router's reservation.  Refusing here is better than a
        # route rejection: it says which call was too big and why.
        raise DebateDraftRefused(
            f"the verifier prompt is {len(prompt.encode('utf-8'))} bytes, "
            f"over the {MAX_PROMPT_BYTES} bound"
        )
    return prompt


def parse_verdict(text: Any, debates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The verdict, or a refusal.  Verdict only: the verifier never rewrites."""

    if not isinstance(text, str) or not text.strip():
        raise DebateDraftRefused("the verifier returned nothing")
    parsed = unwrap_json_object(text)
    if parsed is None:
        raise DebateDraftRefused("the verifier did not return one JSON object")
    if set(parsed) != {"verdict", "findings"}:
        raise DebateDraftRefused(
            f"the verdict must be exactly verdict and findings; got {sorted(parsed)}"
        )
    if parsed["verdict"] not in VERIFIER_VERDICTS:
        raise DebateDraftRefused(f"invalid verdict: {parsed['verdict']!r}")
    rows = parsed["findings"]
    if not isinstance(rows, list):
        raise DebateDraftRefused("findings must be an array")
    known = {debate["debate_ref"] for debate in debates}
    findings: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping) or set(raw) != {"debate_ref", "code", "detail"}:
            raise DebateDraftRefused("each finding is exactly debate_ref, code and detail")
        if raw["debate_ref"] not in known:
            raise DebateDraftRefused(
                f"the verifier named debate {raw['debate_ref']!r}, which is not under review"
            )
        if raw["code"] not in VERIFIER_FINDING_CODES:
            raise DebateDraftRefused(f"unknown finding code: {raw['code']!r}")
        findings.append({
            "debate_ref": str(raw["debate_ref"]), "code": str(raw["code"]),
            "detail": _text_field(raw["detail"], "finding.detail", MAX_STATEMENT_OUT_CHARS),
        })
    if parsed["verdict"] == "pass" and findings:
        raise DebateDraftRefused("a pass verdict cannot carry findings")
    if parsed["verdict"] == "reject" and not findings:
        raise DebateDraftRefused("a reject verdict must say what is wrong")
    return {"verdict": parsed["verdict"], "findings": findings}


# ---------------------------------------------------------------------------
# independence
# ---------------------------------------------------------------------------

def route_family(router_db: str | Path, route_decision_ref: Any) -> str | None:
    """The model family a route decision actually served, or ``None``.

    Read from the routing authority rather than asserted by the caller: the
    whole value of the predicate is that neither call gets to say which family
    it was.
    """

    if not isinstance(route_decision_ref, str) or not route_decision_ref.strip():
        return None
    try:
        connection = sqlite3.connect(f"file:{router_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT decision_json FROM model_route_decisions WHERE decision_id=?",
            (route_decision_ref.strip(),),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        decision = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    endpoint = decision.get("selected_endpoint") or {}
    family = endpoint.get("family")
    return family if isinstance(family, str) and family else None


def independent(
    producer: Mapping[str, Any], verifier: Mapping[str, Any]
) -> tuple[bool, str | None]:
    """D2's predicate, fail-closed.

    Two ways to fail and one of them is silence: a verification whose family
    could not be established is refused, because "we could not tell" and "they
    were different" must never produce the same outcome.
    """

    if not producer.get("work_order_ref") or not verifier.get("work_order_ref"):
        return False, "producer and verifier must both be recorded calls"
    if producer.get("work_order_ref") == verifier.get("work_order_ref"):
        return False, "producer and verifier must be different calls"
    producer_family = producer.get("model_family")
    verifier_family = verifier.get("model_family")
    if not producer_family or not verifier_family:
        return False, "the model family of one of the calls could not be established"
    if producer_family == verifier_family:
        return False, (
            f"producer and verifier both ran on model family {producer_family!r}"
        )
    return True, None


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _provenance(call: Mapping[str, Any], family: str | None) -> dict[str, Any]:
    return {
        "kind": "model",
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "model_family": family,
    }


def draft_debate_map(
    *,
    table: Mapping[str, Any],
    method: Mapping[str, Any],
    model: Any,
    mission: Mapping[str, Any],
    created_at: str,
    previous: Mapping[str, Any] | None = None,
    family_of: Callable[[Any], str | None] | None = None,
    policy: Mapping[str, Any] = DEBATE_POLICY,
) -> dict[str, Any]:
    """One drafting call, one verifying call, and a publishable result or not.

    Never writes.  It returns what a caller may publish and why, so that the
    decision to publish stays with the lane and the authority stays a
    mechanism.
    """

    resolve = family_of or (lambda ref: None)
    prompt = build_prompt(table)
    result: dict[str, Any] = {
        "status": "failed",
        "subject_ref": table["subject_ref"],
        "prompt_bytes": len(prompt.encode("utf-8")),
        "cost_micros": 0,
        "debates": [],
        "rejected": [],
        "reason": None,
        "drafted_by": None,
        "verified_by": None,
    }
    if result["prompt_bytes"] > MAX_PROMPT_BYTES:
        result.update({"status": "refused",
                       "reason": f"the input table is {result['prompt_bytes']} bytes, "
                                 f"over the {MAX_PROMPT_BYTES} bound"})
        return result
    try:
        call = model.call(
            purpose=PURPOSE,
            request_id=prompt_drafter("", prompt)[1][:32],
            prompt=prompt, mission=mission,
        )
    except CockpitModelError as exc:
        result.update({"status": "model_unavailable",
                       "reason": f"{type(exc).__name__}: {exc}"})
        return result
    result["cost_micros"] += int(call.get("cost_micros") or 0)
    drafted_by = _provenance(call, resolve(call.get("route_decision_ref")))
    result["drafted_by"] = drafted_by
    try:
        candidates = parse_draft(call.get("text"), table)
    except DebateDraftError as exc:
        result.update({"status": "refused", "reason": f"{type(exc).__name__}: {exc}"})
        return result

    from .debate_map import cited_refs

    known = {row["debate_ref"] for row in table["previous_debates"]}
    screened = screen_candidates(
        candidates, method=method,
        driver_refs=[row["driver_ref"] for row in table["drivers"]],
        claims=table["claim_index"], known_debate_refs=known,
        prior_refs=cited_refs(previous or {}), policy=policy,
        observed_at=created_at,
    )
    result["rejected"] = screened["rejected"]
    if not screened["admitted"]:
        result.update({
            "status": "no_admitted_debates",
            "reason": "every candidate was refused by the constitution gate",
        })
        return result
    debates = assemble_debates(screened["admitted"], previous=previous, created_at=created_at)
    try:
        verifier_prompt = build_verifier_prompt(table, debates)
    except DebateDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    try:
        check = model.call(
            purpose=PURPOSE,
            request_id=content_hash({"verify": [d["debate_ref"] for d in debates],
                                     "subject": table["subject_ref"]})[:32],
            prompt=verifier_prompt, mission=mission,
        )
    except CockpitModelError as exc:
        result.update({"status": "unverified",
                       "reason": f"the verifying call did not run: {exc}"})
        return result
    result["cost_micros"] += int(check.get("cost_micros") or 0)
    verified_by = _provenance(check, resolve(check.get("route_decision_ref")))
    result["verified_by"] = verified_by
    ok, reason = independent(drafted_by, verified_by)
    if not ok:
        result.update({"status": "not_independent", "reason": reason})
        return result
    try:
        verdict = parse_verdict(check.get("text"), debates)
    except DebateDraftError as exc:
        result.update({"status": "unverified", "reason": f"{type(exc).__name__}: {exc}"})
        return result
    if verdict["verdict"] != "pass":
        result.update({
            "status": "verifier_rejected",
            "reason": "; ".join(
                f"{item['debate_ref']}: {item['code']}" for item in verdict["findings"]
            ),
            "findings": verdict["findings"],
        })
        return result
    result.update({"status": "verified", "debates": debates, "reason": None})
    return result


def change_evidence(
    debates: Sequence[Mapping[str, Any]], previous: Mapping[str, Any] | None
) -> list[str]:
    """The refs that occasioned this version, per ADR-0008.

    The refs a published version cites that the current one does not.  When
    there is no previous version everything is new, and when there is nothing
    new the authority refuses the publication -- which is the point.
    """

    from .debate_map import cited_refs

    fresh = cited_refs({"debates": list(debates)})
    if previous is not None:
        fresh = fresh - cited_refs(previous)
    return sorted(fresh)


def change_reason_for(previous: Mapping[str, Any] | None) -> str:
    """``evidence_thicker`` is the only reason a drafting run can honestly give.

    The others in ADR-0008's vocabulary are assertions about the world -- a
    filing landed, a driver moved, a person decided -- and a lane that reads
    Claims and redraws a map knows only that there is more evidence than there
    was.  Naming one of the others would be a lie the version chain would then
    preserve.
    """

    assert set(CHANGE_REASONS)  # the vocabulary is the ADR's, not this module's
    return "evidence_thicker"


# ---------------------------------------------------------------------------
# reading the inputs out of a Core
# ---------------------------------------------------------------------------

def subject_claim_rows(store: Any, subject_ref: str) -> list[dict[str, Any]]:
    """Canonical Claims about one subject, with everything the tiers need.

    ``query_company_research`` already answers "the current Claims about this
    subject, one copy of each fact" -- P12b's index is what makes the second
    half of that sentence true -- so this only adds the provenance the debate
    layer needs and P12b did not have to store: which document said it, who
    published that document, and the Ledger's own independence group.

    On a Core with no Claim index the rows come back without an aspect, the
    pre-pass finds nothing, and the table is ordered by ref.  That is the
    honest degradation: "this Core has not been indexed" is a different answer
    from "nothing is contested", and neither is an error.
    """

    from .claim_index_tagging import ProvenanceResolver
    from .company_research_view import query_company_research

    rows = query_company_research(store, company_ref=subject_ref, limit=1000)
    provenance = ProvenanceResolver(store.connection)
    titles = _document_titles(store.connection)
    attribution = document_attribution(store.connection)
    enriched: list[dict[str, Any]] = []
    for row in rows:
        ref = row["claim_version_ref"]
        origin = provenance.resolve(ref)
        document_ref = origin.get("document_ref")
        attributed = attribution.get(document_ref or "", {})
        enriched.append({
            **row,
            "importance": row.get("importance") or origin.get("importance"),
            "spec_ref": origin.get("spec_ref"),
            "document_ref": document_ref,
            "document_title": attributed.get("title") or titles.get(document_ref or ""),
            "document_authors": attributed.get("authors"),
            "document_sources": attributed.get("sources"),
            "document_publisher": attributed.get("publisher"),
            "host": attributed.get("host"),
        })
    return enriched


def subject_claim_refs(store: Any, subject_ref: str) -> list[str]:
    """Just the canonical claim version refs for one subject.

    The lane asks every subject every tick whether its evidence moved, and the
    answer is a hash of this list.  Building the full drafting rows to get it
    -- a provenance resolver, a title map and a per-claim evidence walk for
    five companies, five times a minute -- was work done to learn nothing on
    the overwhelmingly common tick where nothing changed.
    """

    from .company_research_view import query_company_research

    return [
        row["claim_version_ref"]
        for row in query_company_research(store, company_ref=subject_ref, limit=1000)
    ]


def _document_titles(connection: Any) -> dict[str, str]:
    """Document ref -> title, for the publisher table to match against.

    The document index is a disposable projection and may not exist; an absent
    title only means a claim falls one rung down the source-identity ladder,
    which is exactly what the ladder is for.
    """

    try:
        rows = connection.execute(
            "SELECT source_record_refs_json, artifact_ref, title "
            "FROM document_index_documents"
        ).fetchall()
    except sqlite3.Error:
        return {}
    titles: dict[str, str] = {}
    for row in rows:
        title = row["title"]
        if not isinstance(title, str) or not title:
            continue
        titles[row["artifact_ref"]] = title
        try:
            for item in json.loads(row["source_record_refs_json"]) or []:
                if isinstance(item, str):
                    titles[item] = title
        except (TypeError, ValueError):
            continue
    return titles


# What a discovered-document row may be able to tell us about who published it.
# ``host`` is P9d-13's and has been there since; the rest are the columns the
# extraction-throughput slice is adding additively as it persists broker
# attribution read from AlphaEngine's document metadata. The names are not
# settled yet and this must not break on a Core that has none of them, so the
# reader takes whichever exist and ignores the rest -- an additive column is a
# capability that appears, never a migration this module has to be taught.
_ATTRIBUTION_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "publisher": ("publisher", "broker", "publisher_ref", "attributed_publisher",
                  "source_publisher"),
    "authors": ("authors", "document_authors", "author"),
    "sources": ("sources", "document_sources", "source_name"),
    "title": ("title", "document_title"),
    "host": ("host",),
}


def document_attribution(connection: Any) -> dict[str, dict[str, Any]]:
    """Document ref -> whatever the Core can say about who published it.

    Three things feed the source ladder from here.  ``publisher`` is an
    attribution the acquisition layer read off the document itself and is
    taken at its word.  ``authors``, ``sources`` and ``title`` are text the
    frozen publisher table is matched against -- AlphaEngine titles carry the
    house as a prefix.  ``host`` is the URL host P9d-13 records for web
    documents, and is the only publisher identity a web page has in the Core.

    None of these columns is required.  On today's live Core only ``host``
    exists, so every broker note falls to the ``document`` basis, which
    ``counting_bases`` refuses to count -- which is why this seam matters more
    than it looks: it is the difference between "no sell-side debate can ever
    open" and "TD and Wolfe are two voices".
    """

    try:
        present = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(coverage_mission_discovered_documents)"
            )
        }
    except sqlite3.Error:
        return {}
    if not present:
        return {}
    chosen = {
        field: next((name for name in names if name in present), None)
        for field, names in _ATTRIBUTION_COLUMNS.items()
    }
    selected = {field: name for field, name in chosen.items() if name}
    if not selected:
        return {}
    columns = ", ".join(f"{name} AS {field}" for field, name in selected.items())
    try:
        rows = connection.execute(
            f"SELECT document_ref, {columns} FROM coverage_mission_discovered_documents"
        ).fetchall()
    except sqlite3.Error:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = {
            field: row[field] for field in selected
            if isinstance(row[field], str) and row[field].strip()
        }
        if values:
            found[row["document_ref"]] = values
    return found


def subject_driver_rows(
    store: Any, subject_ref: str, constitution: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """The drivers a debate about this subject may bind to.

    Two sources, both already governed: the industry driver pack the active
    constitution binds, and the company's own model specification.  The
    specification's refs are local to one company (``rev_bookings``), so they
    are namespaced on the way in -- two companies naming a driver the same way
    is normal and must not make them the same driver.
    """

    drivers: list[dict[str, Any]] = []
    seen: set[str] = set()
    binding = ((constitution or {}).get("bindings") or {}).get("driver_pack_version") or {}
    pack_ref = binding.get("ref")
    if pack_ref:
        row = store.connection.execute(
            "SELECT record_json FROM driver_pack_versions WHERE version_id=?",
            (pack_ref,),
        ).fetchone()
        if row is not None:
            try:
                pack = json.loads(row["record_json"])
            except (TypeError, ValueError):
                pack = {}
            for driver in pack.get("drivers") or []:
                ref = driver.get("driver_ref")
                if isinstance(ref, str) and ref and ref not in seen:
                    seen.add(ref)
                    drivers.append({
                        "driver_ref": ref,
                        "label": str(driver.get("label") or ""),
                        "mechanism": str(driver.get("mechanism") or ""),
                    })
    try:
        spec = store.connection.execute(
            "SELECT revenue_drivers_json FROM coverage_mission_company_model_specs "
            "WHERE company_ref=? ORDER BY created_at DESC, spec_id DESC LIMIT 1",
            (subject_ref,),
        ).fetchone()
    except sqlite3.Error:
        spec = None
    if spec is not None:
        try:
            rows = json.loads(spec["revenue_drivers_json"]) or []
        except (TypeError, ValueError):
            rows = []
        for driver in rows:
            local = driver.get("ref") if isinstance(driver, Mapping) else None
            if not isinstance(local, str) or not local:
                continue
            ref = f"model-driver:{subject_ref}:{local}"
            if ref in seen:
                continue
            seen.add(ref)
            drivers.append({
                "driver_ref": ref,
                "label": str(driver.get("label") or ""),
                "mechanism": str(driver.get("because") or ""),
            })
    return drivers


__all__ = [
    "MAX_CLAIM_ROWS",
    "MAX_COST_USD",
    "MAX_DEBATES",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PROMPT_BYTES",
    "MAX_STATEMENT_CHARS",
    "PURPOSE",
    "SCHEMA_VERSION",
    "TASK_HASH",
    "TASK_REF",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "DebateDraftError",
    "DebateDraftRefused",
    "assemble_debates",
    "DRAFT_CONTRACT_HASH",
    "DRAFT_CONTRACT_VERSION",
    "build_input_table",
    "build_prompt",
    "build_verifier_prompt",
    "change_evidence",
    "change_reason_for",
    "debate_ref_for",
    "draft_debate_map",
    "independent",
    "parse_draft",
    "parse_verdict",
    "prompt_drafter",
    "route_family",
    "document_attribution",
    "subject_claim_refs",
    "subject_claim_rows",
    "subject_driver_rows",
]
