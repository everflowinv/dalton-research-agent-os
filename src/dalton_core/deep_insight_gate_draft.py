"""P12d drafting: one bounded call per question group, verified before believed.

The gate does not read the Ledger.  It reads the *file*: P12a's dossier, whose
sections already carry the canonical Claims with the text that was shown, P12c's
DebateMap, whose open debates are the eighth question almost verbatim, and the
numbers the model and valuation layers hold.  Drafting the gate from raw Claims
would have been a second, competing act of aggregation, and the two would
disagree about the same company in the same week.

The prompt is a **table**, for the reason ``company_model_spec`` found and every
drafter since has repeated: the same content as JSON objects costs several times
the bytes in repeated keys, and the router reserves budget against the size of
the prompt.  So the material is ``tag<TAB>period<TAB>grade<TAB>text`` and the
reply is the only JSON in the exchange.

Four calls, not twelve.  The four industry questions read the same rows, so a
call per question would pay four times for one table; the groups are the
material's shape rather than the numbering's.

What comes back is refused whole on any deviation -- a tag that was not shown, a
question that was not asked, a question missing, more sentences than the cap, a
sentence citing nothing, an answered question with no confidence, an unknown
that does not say what would settle it.  Never repaired: a reply that invents a
tag is not a reply that read the table, and the answers it happened to get right
came out of the same reply.

**The verifier is a different model family** (D2), enforced after the calls by
reading both route decisions, because the family that served is a fact about the
route and not about what we asked for -- and it fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cockpit_model import CockpitModelError, register_purpose, unwrap_json_object
from .company_dossier import (
    CLASSIFICATION_DEFINITIONS,
    INDUSTRY_CLASSIFICATIONS,
    section_body,
)
# The independence predicate is D2's, and it is one predicate: the dossier, the
# debate map and the gate are all cognition-layer outputs checked by something
# that is not the thing that wrote them, and three copies of "different family"
# is how a shared rule stops being one.
from .company_dossier_draft import (  # noqa: F401 - re-exported for the child
    independence,
    independence_precheck,
    router_family_resolver,
)
from .deep_insight_gate import (
    CONFIDENCE_LEVELS,
    GROUPS,
    GROUP_QUESTIONS,
    MAX_GAPS,
    MAX_SOURCES_PER_ANSWER,
    QUESTION_REFS,
    SENTENCES_PER_ANSWER,
    DeepInsightGateValidationError,
    answer_body,
    validate_answer,
)
from .store import content_hash

# P14-0's registry: a lane names its own purpose from its own module rather than
# editing a set in ``cockpit_model``.  Registered at import because importing
# this module is what makes the drafter reachable.
DRAFT_PURPOSE = register_purpose("deep_insight_gate")

# The gate drafts on the deliverable-drafting configuration, which is already in
# the registry: same route, same broker, same day ledger as the dossier it reads
# and the Initial Screen below it.  A separate configuration would only be worth
# its wiring if the gate needed its own rate limit.
MODEL_CONFIG_NAME = "initial-screen-model-config.json"

# Bounded like ``company_model_cli``'s constants and for the same reason: the
# router estimates on prompt bytes, so a bound that looks frugal buys nothing
# but a refusal.  One call is one group of one company.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 3_000
MAX_COST_USD = 0.60
TIMEOUT_SECONDS = 180
# What one run may spend across all its calls, verifier included: four groups
# and one verification, each at the per-call cap.  A run that cannot afford the
# verification publishes nothing rather than publishing unverified.
MAX_RUN_COST_USD = 3.00
MAX_GROUPS_PER_RUN = len(GROUPS)

# What the prompt may carry.
MAX_STATEMENT_ROWS = 44
MAX_NUMBER_ROWS = 24
MAX_DEBATE_ROWS = 12
MAX_ROW_CHARS = 400
MAX_PRIOR_CHARS = 1_200

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
# Closed, and short: a verifier with an open vocabulary writes essays.
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    # an answer asserts more than the rows it cites carry
    "unsupported_answer",
    # a figure in the prose is not in the cited row verbatim
    "number_not_in_source",
    # the answer answers a different question from the one it was asked
    "wrong_question",
    # the draft states an investment conclusion; the gate is a file, not a call
    "conclusion_beyond_evidence",
    # a question was answered where the material shown does not support any
    # answer, which is the failure mode this whole layer exists to catch
    "should_be_unknown",
)


class GateDraftError(RuntimeError):
    """The drafting path failed."""


class GateDraftRefused(GateDraftError):
    """The reply deviated from the contract and was refused whole."""


# ---------------------------------------------------------------------------
# material
# ---------------------------------------------------------------------------


def dossier_rows(
    dossier: Mapping[str, Any], aspects: Sequence[str]
) -> list[dict[str, Any]]:
    """The file's own rows for a set of sections: the sources and the prose.

    Two kinds per section.  The sources are the canonical Claims and filed
    figures the dossier already chose, carried with the text that was shown, so
    the gate cites what the file cites rather than a second selection.  The
    section body is carried as its own citable row, because "the file concludes
    X" is a legitimate thing for a gate answer to rest on and a reader can open
    the section it names.
    """

    wanted = set(aspects)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    version_ref = str(dossier.get("id") or "")
    for section in dossier.get("sections") or []:
        if section["aspect"] not in wanted or section["status"] != "drafted":
            continue
        body = section_body(section)
        if body:
            ref = f"dossier-section:{version_ref}:{section['aspect']}"
            if ref not in seen:
                seen.add(ref)
                rows.append({
                    "kind": "dossier_section", "ref": ref,
                    "text": body[:MAX_ROW_CHARS], "period": None,
                    "importance": "dossier",
                })
        for row in section.get("sources") or []:
            if row["ref"] in seen:
                continue
            seen.add(row["ref"])
            rows.append({
                "kind": row["kind"], "ref": row["ref"],
                "text": str(row["text"])[:MAX_ROW_CHARS], "period": row.get("period"),
                "importance": section["aspect"],
            })
    return rows


def block_rows(dossier: Mapping[str, Any], block: str) -> list[dict[str, Any]]:
    """The classification or the variant view, as citable rows.

    The variant view is the whole of the eighth and eleventh questions on this
    Core today: it is where "what the market is paying for" and "where we think
    it is wrong" already live, and a gate that re-derived them from the same
    Claims would produce a second opinion nobody asked for.
    """

    key = ("industry_classification" if block == "classification" else "variant_view")
    record = dossier.get(key) or {}
    version_ref = str(dossier.get("id") or "")
    rows: list[dict[str, Any]] = []
    body = section_body(record)
    if body:
        rows.append({
            "kind": "dossier_section", "ref": f"dossier-section:{version_ref}:{key}",
            "text": body[:MAX_ROW_CHARS], "period": None, "importance": block,
        })
    for row in record.get("sources") or []:
        rows.append({
            "kind": row["kind"], "ref": row["ref"],
            "text": str(row["text"])[:MAX_ROW_CHARS], "period": row.get("period"),
            "importance": block,
        })
    return rows


def debate_rows(map_version: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The live debates, one row each, bull and bear and market in the text.

    ``open`` and ``shifting`` only -- a ``candidate`` is a question this system
    has not yet found two independent voices on, and putting it beside a real
    disagreement in the gate's eighth answer would misprice both.
    """

    if not map_version:
        return []
    from .debate_map import LIVE_STATUSES

    version_ref = str(map_version.get("id") or "")
    rows: list[dict[str, Any]] = []
    for debate in map_version.get("debates") or []:
        if debate["status"] not in LIVE_STATUSES:
            continue
        market = debate.get("market_position") or {}
        parts = [
            f"争议：{debate['question']}",
            f"多头：{(debate.get('bull_position') or {}).get('statement') or '-'}",
            f"空头：{(debate.get('bear_position') or {}).get('statement') or '-'}",
        ]
        if market.get("available"):
            parts.append(f"市场（{market.get('lean')}）：{market.get('statement')}")
        else:
            parts.append("市场：没有材料说明市场站在哪一边")
        rows.append({
            "kind": "debate",
            "ref": f"debate:{version_ref}:{debate['debate_ref']}",
            "text": "；".join(parts)[:MAX_ROW_CHARS],
            "period": debate.get("first_seen_at"),
            "importance": debate["status"],
        })
    return rows[:MAX_DEBATE_ROWS]


def rejected_debate_notes(map_version: Mapping[str, Any] | None) -> list[str]:
    """Questions the Constitution's admission gate turned away, as plain notes.

    Not citable rows: a rejected candidate is not evidence of anything.  They
    are shown to the twelfth question because "what is still unknown and what
    would settle it cheapest" is exactly the list of questions this system
    wanted to ask and could not yet support.
    """

    if not map_version:
        return []
    return [
        f"{item['question']}（{', '.join(item['reasons'])}）"
        for item in (map_version.get("rejected_by_constitution") or [])
    ][:MAX_DEBATE_ROWS]


def dossier_gap_notes(dossier: Mapping[str, Any]) -> list[str]:
    """Every gap the file already knows it has, with the section that owns it."""

    notes: list[str] = []
    for section in dossier.get("sections") or []:
        for gap in section.get("gaps") or []:
            notes.append(f"{section['aspect']}：{gap}")
        if section["status"] == "unavailable":
            notes.append(f"{section['aspect']}：整节缺，原因是 {section['reason']}")
    for key in ("industry_classification", "variant_view"):
        for gap in (dossier.get(key) or {}).get("gaps") or []:
            notes.append(f"{key}：{gap}")
    return notes[:MAX_GAPS * 4]


def material_rows(
    statements: Sequence[Mapping[str, Any]] = (),
    numbers: Sequence[Mapping[str, Any]] = (),
    debates: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Tag the material: ``C``₁..ₙ statements, ``N``₁..ₙ figures, ``D``₁..ₙ debates.

    Three families rather than one because the tables they render into are
    three, and a reply that cited ``C7`` when it meant the seventh debate would
    otherwise resolve silently to a statement.
    """

    rows: list[dict[str, Any]] = []
    for index, row in enumerate(list(statements)[:MAX_STATEMENT_ROWS], start=1):
        rows.append({**dict(row), "tag": f"C{index}"})
    for index, row in enumerate(list(numbers)[:MAX_NUMBER_ROWS], start=1):
        rows.append({**dict(row), "tag": f"N{index}"})
    for index, row in enumerate(list(debates)[:MAX_DEBATE_ROWS], start=1):
        rows.append({**dict(row), "tag": f"D{index}"})
    tags = {row["tag"] for row in rows}
    if len(tags) != len(rows):
        raise DeepInsightGateValidationError("material tags must be unique")
    refs = [row.get("ref") for row in rows]
    if any(not row.get("ref") or not row.get("text") for row in rows):
        raise DeepInsightGateValidationError("every material row needs a ref and a text")
    if len(set(refs)) != len(refs):
        raise DeepInsightGateValidationError("material rows must not repeat a ref")
    return rows


def render_material(rows: Sequence[Mapping[str, Any]]) -> str:
    statements = [row for row in rows if row["tag"].startswith("C")]
    numbers = [row for row in rows if row["tag"].startswith("N")]
    debates = [row for row in rows if row["tag"].startswith("D")]
    lines: list[str] = []
    lines.append(f"Statements available ({len(statements)}) -- tag, period, source, text:")
    for row in statements:
        lines.append("\t".join([
            row["tag"], str(row.get("period") or "-"),
            str(row.get("importance") or "-"), str(row["text"]),
        ]))
    lines.append("")
    lines.append(f"Figures available ({len(numbers)}) -- tag, period, kind, text:")
    for row in numbers:
        lines.append("\t".join([
            row["tag"], str(row.get("period") or "-"), str(row["kind"]), str(row["text"]),
        ]))
    lines.append("")
    lines.append(f"Live debates ({len(debates)}) -- tag, status, text:")
    for row in debates:
        lines.append("\t".join([
            row["tag"], str(row.get("importance") or "-"), str(row["text"]),
        ]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

GROUP_PURPOSE: Mapping[str, str] = {
    "industry": "这家公司属于哪一类生意，需求与供给由什么决定，行业的长期趋势在什么条件下会改变",
    "company": "这家公司凭什么赢，管理层过去把钱花到哪里、承诺兑现得怎么样",
    "market": "过去五到十年股价是被什么驱动的，今天的共识与多空逻辑各是什么",
    "thesis": "什么指标能证伪我们的判断，判断成立与不成立时分别会发生什么，还剩哪些未知",
}


def build_group_prompt(
    *,
    group: str,
    questions: Mapping[str, str],
    material: Sequence[Mapping[str, Any]],
    company: Mapping[str, Any],
    prior_answers: Mapping[str, str] | None = None,
    notes: Sequence[str] = (),
) -> str:
    """One group's prompt: the questions, the rules, the material, last time's."""

    refs = list(GROUP_QUESTIONS[group])
    lines = [
        "You are answering part of the Deep Insight Gate for a fundamental,",
        "long-biased fund. The gate is the human checkpoint between a first",
        "screen and full coverage: a portfolio manager reads your answers and",
        "decides whether this company is worth covering deeply. Write in Chinese.",
        "You are not advising and not recommending: the gate says what is true",
        "about a company; the decision about what to do with it is made elsewhere.",
        "",
        f"Company: {company.get('ticker') or ''} ({company.get('company_ref')})",
        f"Part: {group} -- {GROUP_PURPOSE.get(group, group)}",
        "",
        "Answer every question below, in this order, and answer no others:",
    ]
    for ref in refs:
        lines.append(f"  {ref}\t{questions[ref]}")
    lines += [
        "",
        "Hard rules:",
        "- Use ONLY the tagged material below. Cite by tag in the refs array.",
        "- NEVER write a C, N or D tag inside a sentence's text: not as a word, not",
        "  in brackets, not in a source list. The tags travel in refs; a sentence",
        "  whose subject is a tag becomes a sentence with no subject once the tag",
        "  is gone.",
        "- Every sentence must cite at least one tag. A sentence you cannot cite is",
        "  a sentence you may not write.",
        "- Copy any figure verbatim from the tag that carries it. Do not convert",
        "  units or scales, do not round, do not recompute a percentage.",
        f"- At most {SENTENCES_PER_ANSWER} sentences per question.",
        "- If the material does not answer a question, return it as",
        '  {"question_ref": "<id>", "status": "unknown", "missing": "<what is not',
        '  known>", "evidence_that_would_answer": "<the cheapest observation or',
        '  document that would settle it, and where it would come from>", "gaps": []}.',
        "  An honest unknown is worth more than a plausible paragraph. Most of these",
        "  questions are expected to be unknown on a thin file, and saying so is the",
        "  answer the reader needs.",
        "- An answered question carries a confidence of "
        + "/".join(CONFIDENCE_LEVELS) + ", and it grades the evidence, not the prose.",
        "- Do not repeat the previous answer. Say what the new evidence changes.",
        "",
    ]
    if "q1" in refs:
        # A table, and deliberately not indented like the question list above:
        # the questions are the structure and the vocabulary is a menu, and a
        # reply that read the menu as structure would answer six questions.
        lines.append("For q1, choose exactly one classification from this closed list")
        lines.append("and return it as the reply's \"classification\" field:")
        for word in INDUSTRY_CLASSIFICATIONS:
            lines.append(f"{word}\t{CLASSIFICATION_DEFINITIONS[word]}")
        lines.append("")
    lines.append("Return raw JSON only, no markdown fence:")
    lines.append('{"answers": [{"question_ref": "<id>", "status": "answered",')
    lines.append('   "confidence": "medium", "sentences": [{"text": "<one sentence>",')
    lines.append('   "refs": ["C3","N1"]}], "gaps": ["<what is missing>"]}]'
                 + (', "classification": "<one word from the list>"}'
                    if "q1" in refs else "}"))
    lines.append("")
    for ref in refs:
        body = (prior_answers or {}).get(ref)
        if body:
            lines.append(f"Previous answer to {ref}: {body[:MAX_PRIOR_CHARS]}")
    if prior_answers:
        lines.append("")
    if notes:
        lines.append("Gaps the file already knows about (context, not citable):")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    lines.append(render_material(material))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# the reply
# ---------------------------------------------------------------------------

_ANSWERED_KEYS = {"question_ref", "status", "confidence", "sentences", "gaps"}
_UNKNOWN_KEYS = {"question_ref", "status", "missing", "evidence_that_would_answer",
                 "gaps"}


def _resolve(row: Mapping[str, Any], material: Sequence[Mapping[str, Any]],
             *, group: str) -> Mapping[str, Any]:
    by_tag = {item["tag"]: item for item in material}
    found = by_tag.get(str(row))
    if found is None:
        raise GateDraftRefused(
            f"{group}: the reply cites {row!r}, which was not shown; the batch is "
            "refused whole")
    return found


def parse_group_output(
    text: str,
    *,
    group: str,
    questions: Mapping[str, str],
    material: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate one group's reply against the closed contract, or refuse whole."""

    value = unwrap_json_object(text)
    if value is None:
        raise GateDraftRefused(f"{group}: the reply is not a JSON object")
    refs = list(GROUP_QUESTIONS[group])
    expected = {"answers"} | ({"classification"} if "q1" in refs else set())
    if set(value) != expected:
        raise GateDraftRefused(
            f"{group}: the reply has keys {sorted(value)}; the contract is "
            f"{sorted(expected)}")
    rows = value["answers"]
    if not isinstance(rows, list) or len(rows) != len(refs):
        raise GateDraftRefused(
            f"{group}: the reply answers {len(rows or [])} of {len(refs)} questions; "
            "every question is answered or explicitly unknown")
    answers: dict[str, Any] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise GateDraftRefused(f"{group}: answers[{index}] is not an object")
        question_ref = str(row.get("question_ref") or "")
        if question_ref != refs[index]:
            raise GateDraftRefused(
                f"{group}: answers[{index}] is {question_ref!r}; this group asks "
                f"{refs[index]!r} in this position")
        status = row.get("status")
        keys = set(row)
        if status == "unknown":
            if keys != _UNKNOWN_KEYS:
                raise GateDraftRefused(
                    f"{group}: {question_ref} has keys {sorted(keys)}; an unknown "
                    f"answer is {sorted(_UNKNOWN_KEYS)}")
            block = {
                "question_ref": question_ref, "question": questions[question_ref],
                "group": group, "status": "unknown", "confidence": None,
                "unknown": {
                    "reason": "no_material_shown",
                    "missing": row["missing"],
                    "evidence_that_would_answer": row["evidence_that_would_answer"],
                },
                "sentences": [], "sources": [], "gaps": row.get("gaps") or [],
            }
        elif status == "answered":
            if keys != _ANSWERED_KEYS:
                raise GateDraftRefused(
                    f"{group}: {question_ref} has keys {sorted(keys)}; an answered "
                    f"question is {sorted(_ANSWERED_KEYS)}")
            sentences_in = row["sentences"]
            if not isinstance(sentences_in, list) or not sentences_in:
                raise GateDraftRefused(
                    f"{group}: {question_ref} is answered and writes nothing")
            sentences: list[dict[str, Any]] = []
            cited: dict[str, Mapping[str, Any]] = {}
            for position, item in enumerate(sentences_in):
                if not isinstance(item, Mapping) or set(item) != {"text", "refs"}:
                    raise GateDraftRefused(
                        f"{group}: {question_ref}.sentences[{position}] must be "
                        "text and refs")
                tags = item["refs"]
                if not isinstance(tags, list) or not tags:
                    raise GateDraftRefused(
                        f"{group}: {question_ref}.sentences[{position}] cites nothing")
                resolved: list[str] = []
                for tag in tags:
                    found = _resolve(tag, material, group=group)
                    resolved.append(found["ref"])
                    cited[found["ref"]] = found
                sentences.append({"text": item["text"], "refs": resolved})
            if len(cited) > MAX_SOURCES_PER_ANSWER:
                raise GateDraftRefused(
                    f"{group}: {question_ref} cites {len(cited)} sources; the cap is "
                    f"{MAX_SOURCES_PER_ANSWER}")
            block = {
                "question_ref": question_ref, "question": questions[question_ref],
                "group": group, "status": "answered",
                "confidence": row.get("confidence"), "unknown": None,
                "sentences": sentences,
                "sources": [
                    {"kind": item["kind"], "ref": item["ref"], "text": item["text"],
                     "period": (None if item.get("period") is None
                                else str(item["period"]))}
                    for item in cited.values()
                ],
                "gaps": row.get("gaps") or [],
            }
        else:
            raise GateDraftRefused(
                f"{group}: {question_ref} has status {status!r}; the contract is "
                "answered or unknown")
        try:
            answers[question_ref] = validate_answer(
                block, question_ref, question_ref=question_ref)
        except DeepInsightGateValidationError as exc:
            # Refused, not repaired: a reply that has to be tidied before it can
            # be read is not a reply.
            raise GateDraftRefused(f"{group}: {exc}") from exc
    out: dict[str, Any] = {"answers": answers}
    if "q1" in refs:
        word = value.get("classification")
        if word not in INDUSTRY_CLASSIFICATIONS:
            raise GateDraftRefused(
                f"{group}: classification {word!r} is not one of the five kinds "
                "plus insufficient_evidence")
        out["classification"] = word
    return out


def draft_group(
    model: Any,
    *,
    group: str,
    questions: Mapping[str, str],
    material: Sequence[Mapping[str, Any]],
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
    prior_answers: Mapping[str, str] | None = None,
    notes: Sequence[str] = (),
) -> dict[str, Any]:
    """One bounded call for one group.  Returns its answers or a refusal."""

    prompt = build_group_prompt(
        group=group, questions=questions, material=material, company=company,
        prior_answers=prior_answers, notes=notes,
    )
    request_id = content_hash({
        "group": group, "company": company.get("company_ref"),
        "prompt_sha": content_hash(prompt),
    })[:32]
    try:
        call = model.call(purpose=DRAFT_PURPOSE, request_id=request_id,
                          prompt=prompt, mission=mission)
    except CockpitModelError as exc:
        return {"status": "unavailable", "group": group,
                "reason": f"{type(exc).__name__}: {exc}"}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": bool(call.get("replayed")),
        "cost_micros": int(call.get("cost_micros") or 0),
    }
    try:
        parsed = parse_group_output(
            call["text"], group=group, questions=questions, material=material)
    except GateDraftRefused as exc:
        return {"status": "refused", "group": group, "reason": str(exc),
                "model": provenance}
    return {"status": "drafted", "group": group, "answers": parsed["answers"],
            "classification": parsed.get("classification"), "model": provenance,
            "prompt_bytes": len(prompt.encode("utf-8"))}


# ---------------------------------------------------------------------------
# the independent verifier (D2)
# ---------------------------------------------------------------------------


def draft_hash(answers: Mapping[str, Any]) -> str:
    """What the verifier's verdict is about, by content."""

    return content_hash({key: answers[key] for key in sorted(answers)})


def build_verifier_prompt(
    answers: Mapping[str, Any], *, company: Mapping[str, Any]
) -> str:
    """The verifier reads the answers and the rows they cite, and answers once."""

    lines = [
        "You are an independent verifier. Another model answered the Deep Insight",
        "Gate's questions from a fixed table of evidence. You do not rewrite the",
        "answers, improve them or grade them. You answer one question: does every",
        "answer stay inside the rows it cites?",
        "",
        f"Company: {company.get('ticker') or ''} ({company.get('company_ref')})",
        "",
        "Return raw JSON only, nothing else:",
        '{"verdict": "pass|reject", "findings": [{"question_ref": "<id>",',
        '   "code": "' + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}',
        "A pass verdict must have no findings; a reject verdict must have at least one.",
        "An unknown answer is never a finding: refusing to answer is allowed.",
        "",
    ]
    for ref in QUESTION_REFS:
        answer = answers.get(ref)
        if answer is None:
            continue
        lines.append(f"## {ref} {answer['question']}")
        if answer["status"] == "unknown":
            lines.append(f"- (unknown) {answer['unknown']['missing']}")
            lines.append("")
            continue
        lines.append(f"- confidence: {answer['confidence']}")
        for row in answer["sentences"]:
            lines.append(f"- {row['text']}")
            for source_ref in row["refs"]:
                source = next((item for item in answer.get("sources") or []
                               if item["ref"] == source_ref), None)
                if source is not None:
                    lines.append(f"    cites: {source['text'][:MAX_ROW_CHARS]}")
        lines.append("")
    return "\n".join(lines)


def validate_verifier_output(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GateDraftRefused("the verifier did not return an object")
    if set(value) != {"verdict", "findings"}:
        raise GateDraftRefused(
            f"the verifier returned keys {sorted(value)}; the contract is verdict "
            "and findings")
    verdict = value["verdict"]
    if verdict not in VERIFIER_VERDICTS:
        raise GateDraftRefused(f"invalid verdict: {verdict!r}")
    rows = value["findings"] or []
    if not isinstance(rows, list):
        raise GateDraftRefused("findings must be a list")
    findings = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"question_ref", "code", "detail"}:
            raise GateDraftRefused(
                "each finding must be exactly question_ref, code and detail")
        if row["code"] not in VERIFIER_FINDING_CODES:
            raise GateDraftRefused(f"unknown finding code: {row['code']!r}")
        detail = str(row["detail"] or "").strip()
        if not detail or len(detail) > 300 or "\n" in detail:
            raise GateDraftRefused("a finding's detail is one short sentence")
        findings.append({"question_ref": str(row["question_ref"]),
                         "code": str(row["code"]), "detail": detail})
    if verdict == "pass" and findings:
        raise GateDraftRefused("a pass verdict must have no findings")
    if verdict == "reject" and not findings:
        raise GateDraftRefused("a reject verdict must have at least one finding")
    return {"verdict": verdict, "findings": findings}


def verify(
    model: Any,
    answers: Mapping[str, Any],
    *,
    company: Mapping[str, Any],
    mission: Mapping[str, Any],
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the draft."""

    if not answers:
        return {"status": "skipped", "reason": "nothing was drafted"}
    digest = draft_hash(answers)
    prompt = build_verifier_prompt(answers, company=company)
    try:
        call = model.call(purpose=DRAFT_PURPOSE, request_id=f"verify-{digest[:24]}",
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
    except GateDraftRefused as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {
        "status": "verified",
        # Bound to what was verified: a verdict that does not name the draft it
        # read is a verdict about nothing.
        "verified_draft_hash": digest,
        **validated,
        "model": provenance,
    }


def summarise_answers(answers: Mapping[str, Any]) -> dict[str, Any]:
    """How much the draft actually answered, for the summary and the report."""

    answered = [key for key, item in answers.items() if item["status"] == "answered"]
    return {
        "questions": len(answers),
        "answered": len(answered),
        "unknown": len(answers) - len(answered),
        "cited_sources": sum(len(item.get("sources") or ()) for item in answers.values()),
    }


def rendered_answers(answers: Mapping[str, Any]) -> dict[str, str]:
    return {ref: answer_body(answer) for ref, answer in answers.items()}


__all__ = [
    "DRAFT_PURPOSE",
    "GROUP_PURPOSE",
    "MAX_COST_USD",
    "MAX_DEBATE_ROWS",
    "MAX_GROUPS_PER_RUN",
    "MAX_INPUT_TOKENS",
    "MAX_NUMBER_ROWS",
    "MAX_OUTPUT_TOKENS",
    "MAX_RUN_COST_USD",
    "MAX_STATEMENT_ROWS",
    "MODEL_CONFIG_NAME",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "GateDraftError",
    "GateDraftRefused",
    "block_rows",
    "build_group_prompt",
    "build_verifier_prompt",
    "debate_rows",
    "dossier_gap_notes",
    "dossier_rows",
    "draft_group",
    "draft_hash",
    "independence",
    "independence_precheck",
    "material_rows",
    "parse_group_output",
    "rejected_debate_notes",
    "render_material",
    "rendered_answers",
    "router_family_resolver",
    "summarise_answers",
    "validate_verifier_output",
    "verify",
]
