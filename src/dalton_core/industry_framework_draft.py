"""P12e drafting: one bounded call per unit, verified before it is believed.

The prompt is a **table**, not an object graph, for the reason
``company_model_spec`` found and every drafting lane since has repeated: the
same content encoded as JSON objects costs several times the bytes in repeated
keys, and the router reserves budget against the size of the prompt.  So the
material is ``tag<TAB>period<TAB>grade<TAB>text`` and the reply is the only
JSON in the exchange.

What the model is shown for one unit:

* the **industry-subject canonical Claims** -- ``query_company_research`` with
  the industry ref as the subject and P12b's ``industry`` aspect.  Today that
  set is empty on the live Core: the extraction path that files a Claim against
  the industry rather than a company is on another branch.  A unit with no
  statements and no cells is ``no_industry_claims`` rather than a call;
* the **computed comparison cells**, tagged ``T``₁..ₙ, each carrying the figure
  verbatim in the text the number discipline will check the prose against;
* the per-company dossier ``demand_drivers`` and ``supply_and_cost`` sections,
  which are the closest thing the system has to industry prose that a person
  has already accepted, carried as ``dossier_section`` refs;
* the slot structure, which for a causal-chain section came from the
  Constitution and for a driver block came from the driver pack.

What comes back is refused whole on any deviation -- a tag that was not shown,
a slot that was not asked for, a slot missing, more sentences than the cap, a
sentence citing nothing, a key the contract does not have.  Never repaired.  A
reply that invents a tag is not a reply that read the table, and the rows it
happened to get right came out of the same reply.

**The verifier is a different model family.**  D2 in the plan, and the
machinery is imported from the dossier's drafter rather than copied: the
independence predicate is a property of the system, not of one lane, and two
implementations of it would eventually disagree about what independence is.
It fails closed -- an unresolvable family is not an independent one.

**The comparison table is shown and never asked for.**  The model may describe
it, cite it and disagree with what a sentence elsewhere says about it; it may
not recompute a cell, restate one in different units, or add a row.  That is
enforced twice: the prompt says so, and every figure in the prose has to appear
verbatim in a cited row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cockpit_model import (CockpitModelError, independent_model_call,
                            unwrap_json_object)
# The independence predicate and the verifier's reply contract, imported from
# the dossier's drafter.  D2 is one rule about the system; a second copy of it
# here would be a second definition of what "a different family" means.
from .company_dossier_draft import (
    VERIFIER_FINDING_CODES,
    VERIFIER_VERDICTS,
    draft_hash,
    independence,
    independence_precheck,
    router_family_resolver,
    validate_verifier_output,
)
from .industry_framework import (
    CHARACTERISTIC_FIELDS,
    CHARACTERISTIC_PROMPTS,
    MAX_SOURCES_PER_UNIT,
    REF_KINDS,
    SLOT_SENTENCE_CAP,
    STATIC_UNITS,
    UNIT_SENTENCE_CAP,
    DRIVER_STANCES,
    IndustryFrameworkValidationError,
    unit_body,
    validate_characteristics,
    validate_driver_block,
    validate_section,
)
from .model_fallback_chain import TIER_BRAIN, TIER_VERIFIER, register_purpose_tier
from .store import content_hash

# P14-0's registry: a lane names its own purpose from its own module rather
# than editing a set in ``cockpit_model``.  Registered together with its tier,
# because a purpose with no tier is a call with no fallback chain -- INT2's
# finding -- and ``industry_framework`` already sits in the tier map as brain
# work, which is what forming a view about an industry is.
DRAFT_PURPOSE = "industry_framework"
register_purpose_tier(DRAFT_PURPOSE, TIER_BRAIN)
VERIFIER_PURPOSE = "industry_framework_verifier"
register_purpose_tier(VERIFIER_PURPOSE, TIER_VERIFIER)

# The framework drafts on the deliverable-drafting configuration, which is
# already in the registry: same route, same broker, same day ledger as the
# Initial Screen and the dossier.  A separate configuration would only be worth
# its wiring if this lane needed its own rate limit, and it runs weekly.
MODEL_CONFIG_NAME = "initial-screen-model-config.json"

# Bounded like ``company_model_cli``'s constants and for the same reason: the
# router estimates on prompt bytes, so a bound that looks frugal buys nothing
# but a refusal.  One unit is one link of the chain, or one horizon's drivers.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 3_000
MAX_COST_USD = 0.60
TIMEOUT_SECONDS = 180
# What one tick may spend across all its calls, verifier included.  A framework
# is six links plus three blocks; drafting all nine in one tick would be a
# third of the live mission's daily budget in a lane that runs weekly anyway.
MAX_RUN_COST_USD = 2.50
MAX_UNITS_PER_RUN = 3

# What the prompt may carry.
MAX_CLAIM_ROWS = 30
MAX_CELL_ROWS = 40
MAX_DOSSIER_ROWS = 10
MAX_ROW_CHARS = 400
MAX_PRIOR_CHARS = 2_000

# Which material tag prefix belongs to which ref kind.  Three letters because
# there are three tables in the prompt and a reply that cites ``T3`` has said
# which table it read, which is worth more than a uniform numbering that made
# a mis-citation invisible.
TAG_PREFIXES: Mapping[str, str] = {
    "claim": "C",
    "comparison_cell": "T",
    "dossier_section": "D",
    "figure": "N",
    "debate": "B",
}


class FrameworkDraftError(RuntimeError):
    """The drafting path failed."""


class FrameworkDraftRefused(FrameworkDraftError):
    """The reply deviated from the contract and was refused whole."""


# ---------------------------------------------------------------------------
# material
# ---------------------------------------------------------------------------


def material_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Tag the material for one unit, one numbering per ref kind.

    Rows arrive in the order the caller chose -- importance first for Claims,
    oldest-quarter-first for cells -- and the per-kind bound is applied here so
    that the prompt's size is decided in one place rather than in four queries.
    """

    caps = {"claim": MAX_CLAIM_ROWS, "comparison_cell": MAX_CELL_ROWS,
            "dossier_section": MAX_DOSSIER_ROWS}
    counted: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind not in REF_KINDS:
            raise IndustryFrameworkValidationError(
                f"a material row's kind is one of {list(REF_KINDS)}; got {kind!r}")
        seen = counted.get(kind, 0)
        if seen >= caps.get(kind, MAX_CLAIM_ROWS):
            continue
        counted[kind] = seen + 1
        ref = str(row.get("ref") or "")
        text = str(row.get("text") or "")[:MAX_ROW_CHARS]
        if not ref or not text:
            raise IndustryFrameworkValidationError(
                "every material row needs a ref and a text")
        out.append({
            "tag": f"{TAG_PREFIXES[kind]}{seen + 1}",
            "kind": kind,
            "ref": ref,
            "text": text,
            "period": row.get("period"),
            "importance": row.get("importance"),
        })
    if len({row["tag"] for row in out}) != len(out):
        raise IndustryFrameworkValidationError("material tags must be unique")
    return out


def render_material(rows: Sequence[Mapping[str, Any]]) -> str:
    """The three tables, each labelled by what it is and how to cite it."""

    groups = (
        ("claim", "Statements about the industry", "tag, period, source grade, text"),
        ("comparison_cell", "Computed comparison cells (NOT yours to recompute)",
         "tag, quarter, basis, text"),
        ("dossier_section", "Company file sections already accepted",
         "tag, company, aspect, text"),
    )
    lines: list[str] = []
    for kind, title, header in groups:
        found = [row for row in rows if row["kind"] == kind]
        lines.append(f"{title} ({len(found)}) -- {header}:")
        for row in found:
            lines.append("\t".join([
                row["tag"], str(row["period"] or "-"),
                str(row["importance"] or "-"), row["text"],
            ]))
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------


def _unit_purpose(unit: str, *, link: str = "", title: str = "") -> str:
    if unit.startswith("causal_chain:"):
        return (f"因果链第 {int(unit.split(':')[1]) + 1} 环「{title}」：{link}")
    if unit == "characteristics":
        return ("the industry's own characteristics: what kind of business this "
                "is, how it behaves against the cycle, how far ahead revenue is "
                "visible, what capacity costs, and how share is distributed")
    if unit == "long_term_drivers":
        return "the drivers that decide what this industry is over a cycle"
    return "the drivers that decide what this industry prints in a quarter"


def build_unit_prompt(
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    industry: Mapping[str, Any],
    link: str = "",
    title: str = "",
    prior_body: str = "",
    comparison_table: str = "",
) -> str:
    """One unit's prompt: the slots, the rules, the material, the last version."""

    lines = [
        "You are writing one part of an industry framework for a fundamental,",
        "long-biased fund. It is read by a portfolio manager who knows the sector.",
        "Write in Chinese. You are not advising and not recommending: a framework",
        "says what is true about an industry; what to do about it is decided",
        "elsewhere, by a person.",
        "",
        f"Industry: {industry.get('industry_ref')}",
        f"Covered companies: {', '.join(industry.get('tickers') or ())}",
        f"Part: {unit} -- {_unit_purpose(unit, link=link, title=title)}",
        "",
        "Structure. Fill every slot below, in this order, and invent none:",
    ]
    for slot in structure:
        lines.append(f"  {slot['slot_id']}\t{slot['prompt']}")
    lines += [
        "",
        "Hard rules:",
        "- Use ONLY the tagged material below. Cite by tag in the refs array.",
        "- NEVER write a C, T or D tag inside a sentence's text: not as a word, not",
        "  in brackets, not in a source list. The tags travel in refs; a sentence",
        "  whose subject is a tag becomes a sentence with no subject once the tag",
        "  is gone.",
        "- Every sentence must cite at least one tag. A sentence you cannot cite is",
        "  a sentence you may not write.",
        "- Copy any figure verbatim from the tag that carries it. Do not convert",
        "  units or scales, do not round, do not recompute a percentage, do not",
        "  average two cells into a third number.",
        "- The comparison table is already computed and is not yours to change.",
        "  Describe it, compare across it, disagree with it -- but do not add a row,",
        "  do not fill a cell it says is unavailable, and do not restate a value in",
        "  different units.",
        f"- At most {SLOT_SENTENCE_CAP} sentences in a slot and {UNIT_SENTENCE_CAP}",
        "  in this part.",
        "- If the material does not answer a slot, return that slot as",
        '  {"slot_id": "<id>", "unknown": "<what is missing to answer it>"} instead',
        "  of writing something plausible. An honest unknown is worth more than a",
        "  guess, and it is what the gap list is built from.",
        "- Do not repeat the previous version. Say what the new evidence changes.",
        "",
    ]
    if unit == "characteristics":
        lines.append("Answer each of these with exactly one word from its own list:")
        for field, allowed in CHARACTERISTIC_FIELDS.items():
            lines.append(f"{field}\t{CHARACTERISTIC_PROMPTS[field]}")
            lines.append(f"  choices: {', '.join(allowed)}")
        lines.append("")
        lines.append("Return raw JSON only, no markdown fence:")
        lines.append('{"values": {"classification": "<word>", "cyclicality": "<word>",')
        lines.append('  "revenue_visibility": "<word>", "capital_intensity": "<word>",')
        lines.append('  "concentration": "<word>"},')
        lines.append(' "slots": [{"slot_id": "<id>", "sentences": [{"text": "<one sentence>",')
        lines.append('   "refs": ["C3"]}]}], "gaps": ["<what is missing>"]}')
    elif unit in ("long_term_drivers", "short_term_drivers"):
        lines.append(
            "Take a stance on every driver in the structure. The stances are: "
            + ", ".join(DRIVER_STANCES) + ".")
        lines.append(
            "'unknown' is the honest answer when the material says nothing; a "
            "stance other than 'unknown' needs at least one sentence behind it.")
        lines.append("")
        lines.append("Return raw JSON only, no markdown fence:")
        lines.append('{"stances": {"<driver_ref>": "<stance>"},')
        lines.append(' "slots": [{"slot_id": "driver:<driver_ref>",')
        lines.append('   "sentences": [{"text": "<one sentence>", "refs": ["T4"]}]}],')
        lines.append(' "gaps": ["<what is missing>"]}')
    else:
        lines.append("Return raw JSON only, no markdown fence:")
        lines.append('{"slots": [{"slot_id": "<id>", "sentences": [{"text": "<one sentence>",')
        lines.append('   "refs": ["C3","T1"]}]}], "gaps": ["<what is missing>"]}')
    lines.append("")
    if comparison_table:
        lines += [
            "The computed cross-company table (columns are calendar quarters; each",
            "cell carries its own fiscal period end, which can be up to a month",
            "away from the column):",
            comparison_table, "",
        ]
    if prior_body:
        lines += ["Previous version of this part:", prior_body[:MAX_PRIOR_CHARS], ""]
    lines.append(render_material(material))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the reply
# ---------------------------------------------------------------------------


def _resolve_tags(
    slots: Any, material: Sequence[Mapping[str, Any]], *, unit: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn cited tags into refs, or refuse.  Sources are what was cited."""

    by_tag = {row["tag"]: row for row in material}
    if not isinstance(slots, list) or not slots:
        raise FrameworkDraftRefused(f"{unit}: the reply carries no slots")
    resolved: list[dict[str, Any]] = []
    cited: dict[str, dict[str, Any]] = {}
    for index, slot in enumerate(slots):
        if not isinstance(slot, Mapping):
            raise FrameworkDraftRefused(f"{unit}: slots[{index}] is not an object")
        if set(slot) == {"slot_id", "unknown"}:
            resolved.append(dict(slot))
            continue
        if set(slot) != {"slot_id", "sentences"}:
            raise FrameworkDraftRefused(
                f"{unit}: slots[{index}] has keys {sorted(slot)}; the contract is "
                "slot_id with either sentences or unknown")
        rows = slot["sentences"]
        if not isinstance(rows, list):
            raise FrameworkDraftRefused(f"{unit}: slots[{index}].sentences is not a list")
        sentences = []
        for position, row in enumerate(rows):
            if not isinstance(row, Mapping) or set(row) != {"text", "refs"}:
                raise FrameworkDraftRefused(
                    f"{unit}: slots[{index}].sentences[{position}] must be text and refs")
            tags = row["refs"]
            if not isinstance(tags, list) or not tags:
                raise FrameworkDraftRefused(
                    f"{unit}: slots[{index}].sentences[{position}] cites nothing")
            refs = []
            for tag in tags:
                found = by_tag.get(str(tag))
                if found is None:
                    raise FrameworkDraftRefused(
                        f"{unit}: the reply cites {tag!r}, which was not shown; the "
                        "batch is refused whole")
                refs.append(found["ref"])
                cited[found["ref"]] = found
            sentences.append({"text": row["text"], "refs": refs})
        resolved.append({"slot_id": slot["slot_id"], "sentences": sentences})
    sources = [
        {"kind": row["kind"], "ref": row["ref"], "text": row["text"],
         "period": None if row.get("period") is None else str(row["period"])}
        for row in cited.values()
    ]
    if len(sources) > MAX_SOURCES_PER_UNIT:
        raise FrameworkDraftRefused(
            f"{unit}: the reply cites {len(sources)} sources; the cap is "
            f"{MAX_SOURCES_PER_UNIT}")
    return resolved, sources


def parse_unit_output(
    text: str,
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    link: str = "",
    title: str = "",
    link_index: int = 0,
) -> dict[str, Any]:
    """Validate one reply against the closed contract, or refuse it whole."""

    value = unwrap_json_object(text)
    if value is None:
        raise FrameworkDraftRefused(f"{unit}: the reply is not a JSON object")
    if unit == "characteristics":
        expected = {"values", "slots", "gaps"}
    elif unit in ("long_term_drivers", "short_term_drivers"):
        expected = {"stances", "slots", "gaps"}
    else:
        expected = {"slots", "gaps"}
    if set(value) != expected:
        raise FrameworkDraftRefused(
            f"{unit}: the reply has keys {sorted(value)}; the contract is "
            f"{sorted(expected)}")
    slots, sources = _resolve_tags(value["slots"], material, unit=unit)
    gaps = value.get("gaps") or []
    ids = [slot["slot_id"] for slot in structure]
    try:
        if unit == "characteristics":
            return validate_characteristics({
                "status": "drafted", "reason": None,
                "values": value["values"], "structure": ids, "slots": slots,
                "sources": sources, "gaps": gaps,
            })
        if unit in ("long_term_drivers", "short_term_drivers"):
            return validate_driver_block({
                "horizon": "long_term" if unit == "long_term_drivers" else "short_term",
                "status": "drafted", "reason": None, "structure": ids,
                "stances": value["stances"], "slots": slots, "sources": sources,
                "gaps": gaps,
            }, unit)
        return validate_section({
            "link_index": link_index, "link": link, "title": title,
            "status": "drafted", "reason": None, "structure": ids,
            "slots": slots, "sources": sources, "gaps": gaps,
        }, unit)
    except IndustryFrameworkValidationError as exc:
        # Refused, not repaired: a reply that has to be tidied before it can be
        # read is not a reply.
        raise FrameworkDraftRefused(f"{unit}: {exc}") from exc


def draft_unit(
    model: Any,
    *,
    unit: str,
    structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]],
    industry: Mapping[str, Any],
    mission: Mapping[str, Any],
    link: str = "",
    title: str = "",
    link_index: int = 0,
    prior_body: str = "",
    comparison_table: str = "",
) -> dict[str, Any]:
    """One bounded call for one unit.  Returns the drafted block or a refusal."""

    prompt = build_unit_prompt(
        unit=unit, structure=structure, material=material, industry=industry,
        link=link, title=title, prior_body=prior_body,
        comparison_table=comparison_table,
    )
    request_id = content_hash({
        "unit": unit, "industry": industry.get("industry_ref"),
        "prompt_sha": content_hash(prompt),
    })[:32]
    try:
        call = model.call(purpose=DRAFT_PURPOSE, request_id=request_id,
                          prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "unavailable", "unit": unit,
                "reason": f"{type(exc).__name__}: {exc}"}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
    }
    try:
        block = parse_unit_output(
            call["text"], unit=unit, structure=structure, material=material,
            link=link, title=title, link_index=link_index,
        )
    except FrameworkDraftRefused as exc:
        return {"status": "refused", "unit": unit, "reason": str(exc),
                "model": provenance}
    return {"status": "drafted", "unit": unit, "block": block, "model": provenance,
            "prompt_bytes": len(prompt.encode("utf-8"))}


# ---------------------------------------------------------------------------
# the independent verifier (D2)
# ---------------------------------------------------------------------------


def build_verifier_prompt(
    blocks: Mapping[str, Any], *, industry: Mapping[str, Any]
) -> str:
    """The verifier reads the draft and the rows it cites, and answers once."""

    lines = [
        "You are an independent verifier. Another model drafted parts of an industry",
        "framework from a fixed table of evidence and a computed comparison table.",
        "You do not rewrite it, improve it or grade it. You answer one question:",
        "does every sentence stay inside the rows it cites?",
        "",
        f"Industry: {industry.get('industry_ref')}",
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"unit": "<part>",',
        '   "code": "' + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
        "",
    ]
    for unit in sorted(blocks):
        block = blocks[unit]
        lines.append(f"## {unit}")
        for field, word in (block.get("values") or {}).items():
            lines.append(f"{field}: {word}")
        for driver, stance in (block.get("stances") or {}).items():
            lines.append(f"{driver}: {stance}")
        for slot in block.get("slots") or []:
            if "unknown" in slot:
                lines.append(f"- {slot['slot_id']}: (unknown) {slot['unknown']}")
                continue
            for row in slot["sentences"]:
                lines.append(f"- {slot['slot_id']}: {row['text']}")
                for ref in row["refs"]:
                    source = next((item for item in block.get("sources") or []
                                   if item["ref"] == ref), None)
                    if source is not None:
                        lines.append(f"    cites: {source['text'][:MAX_ROW_CHARS]}")
        lines.append("")
    return "\n".join(lines)


def verify(
    model: Any,
    blocks: Mapping[str, Any],
    *,
    industry: Mapping[str, Any],
    mission: Mapping[str, Any],
    producer_route_decision_refs: Sequence[str | None] = (),
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the draft."""

    if not blocks:
        return {"status": "skipped", "reason": "nothing was drafted"}
    digest = draft_hash(blocks)
    prompt = build_verifier_prompt(blocks, industry=industry)
    try:
        call = independent_model_call(
            model, producer_route_decision_refs=producer_route_decision_refs,
            purpose=VERIFIER_PURPOSE, request_id=f"verify-{digest[:24]}",
            prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
    }
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]))
    except Exception as exc:  # noqa: BLE001 - the dossier's refusal type
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {
        "status": "verified",
        # Bound to what was verified: a verdict that does not name the draft it
        # read is a verdict about nothing.
        "verified_draft_hash": digest,
        **validated,
        "model": provenance,
    }


def summarise_blocks(blocks: Mapping[str, Any]) -> dict[str, Any]:
    """How much prose and how many refs the draft carries, for the summary."""

    sentences = refs = unknowns = 0
    for block in blocks.values():
        for slot in block.get("slots") or []:
            sentences += len(slot.get("sentences") or ())
            unknowns += 1 if "unknown" in slot else 0
        refs += len(block.get("sources") or ())
    return {"units": len(blocks), "sentences": sentences, "cited_sources": refs,
            "unknown_slots": unknowns}


def rendered_bodies(blocks: Mapping[str, Any]) -> dict[str, str]:
    return {unit: unit_body(block) for unit, block in blocks.items()}


__all__ = [
    "DRAFT_PURPOSE",
    "VERIFIER_PURPOSE",
    "MAX_CELL_ROWS",
    "MAX_CLAIM_ROWS",
    "MAX_COST_USD",
    "MAX_DOSSIER_ROWS",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_RUN_COST_USD",
    "MAX_UNITS_PER_RUN",
    "MODEL_CONFIG_NAME",
    "STATIC_UNITS",
    "TAG_PREFIXES",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "FrameworkDraftError",
    "FrameworkDraftRefused",
    "build_unit_prompt",
    "build_verifier_prompt",
    "draft_hash",
    "draft_unit",
    "independence",
    "independence_precheck",
    "material_rows",
    "parse_unit_output",
    "render_material",
    "rendered_bodies",
    "router_family_resolver",
    "summarise_blocks",
    "validate_verifier_output",
    "verify",
]
