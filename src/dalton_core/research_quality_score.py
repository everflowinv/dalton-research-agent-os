"""Q1: score one research artefact against a frozen rubric, in two layers.

The two layers exist because they fail differently.

**The deterministic layer needs no model and cannot be talked out of its
answer.**  Every defect the live Initial Screens actually carry is of this
kind: a number the document cannot trace, citation scaffolding left in the
prose, the same quarter's revenue cited three times because three duplicate
Claims exist for it, a section that is a title and nothing else, a claim ref
that no longer resolves.  These are facts about the artefact.  Asking a model
about them would be slower, dearer and less reliable than reading them, and a
model that says "the citations look fine" is worse than no answer, because it
sounds like one.

**The judge layer reads.**  Whether a thesis identifies a driver, whether an
anti-thesis is a reverse view rather than a risk list, whether the gaps are
honest -- no regex decides those.  One bounded call, the rubric and the
artefact and the cited Claims in the prompt, and an output that is verified
before it is believed: the criterion ids must be exactly the rubric's, the
scores must be in range, each needs one sentence of evidence, and there must be
nothing else in the reply.  Anything else is ``refused`` rather than repaired,
for the same reason the verifier in ``thesis_impact`` returns only a verdict:
a judgement that has to be cleaned up before it can be read is not a judgement.

Both layers land in one append-only ``QualityScoreVersion`` bound to the
artefact's ref *and its content hash* and to the rubric's hash.  The binding is
the point: a score is a statement about one exact document under one exact
standard, and re-scoring the same document under the same standard with the
same model configuration is a ``duplicate``, not a second opinion.  Score
shopping is the failure mode of every quality gate that scores on demand.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .cockpit_model import (
    CockpitModelError,
    independent_model_call,
    lane_status_for,
    register_purpose,
    unwrap_json_object,
)
from .mission_deliverable import unsourced_numbers, value_tokens
from .research_quality_rubrics import (
    DOSSIER_SECTIONS,
    WEEKLY_BRIEF_SECTIONS,
    PASSING_SCORE,
    SCALE,
    SCORE_MAX,
    SCORE_MIN,
    Rubric,
    rubric as get_rubric,
)
from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("research_quality_schema.sql")

# Bumped when a check changes what it decides.  It is part of a score's
# identity, so a rescore after a scorer change is a new version rather than a
# duplicate -- otherwise a fixed check could never be applied to a document
# that had already been scored by the broken one.
SCORER_VERSION = "0.1"

ARTEFACT_KINDS: tuple[str, ...] = (
    "initial_screen", "ask_answer", "company_dossier", "weekly_brief",
)

# P14-0 registry: a lane names its own purpose from its own module rather than
# editing a set in ``cockpit_model``.  "quality" is the system reading its own
# output against a standard -- named rather than folded into "ask" because a
# judge competing with the owner's questions for the same budget line should be
# visible as its own line in the day ledger.
#
# Registered at import because importing this module is what makes the judge
# reachable: anything that can call ``judge()`` has already run this line, and
# a purpose that is registered later than the call that uses it is a purpose
# that is not registered.
JUDGE_PURPOSE = register_purpose("quality")
VERIFIER_PURPOSE = register_purpose("quality_verifier")
# The judge runs on the deliverable-drafting configuration, which is already in
# the registry: it is the same route, the same broker and the same day ledger
# as the drafting it grades, and a separate configuration would only be worth
# its wiring if the judge needed its own rate limit. See the report's
# integration section.
JUDGE_MODEL_CONFIG_NAME = "initial-screen-model-config.json"

# Bounded like company_model_cli's constants, and for the same reason: the
# router estimates on prompt bytes, so a bound that looks frugal buys nothing
# but a refusal.  The judge reads one document and answers with a table.
MAX_INPUT_TOKENS = 120_000
MAX_OUTPUT_TOKENS = 2_000
MAX_COST_USD = 0.60
TIMEOUT_SECONDS = 180
# What the prompt may carry of the artefact and of its evidence.  A screen runs
# to ~12k characters of body; a dossier will run longer.
MAX_ARTEFACT_CHARS = 40_000
MAX_CLAIM_ROWS = 80
MAX_CLAIM_TEXT = 400
MAX_EVIDENCE_CHARS = 300

# Two versions of a section that agree this closely are the same section in
# different words.  0.85 is deliberately high: an author who genuinely revisits
# a section rewrites more than a sixth of it, and a threshold that catches
# honest editing would make the check useless by making it noisy.
RESTATEMENT_SIMILARITY = 0.85

# The Playbook's initial_screen template, frozen here so a golden case can be
# checked without a live playbook record.  ``artefact_from_deliverable`` prefers
# the playbook the document was published under whenever it is given one.
INITIAL_SCREEN_SECTIONS: tuple[str, ...] = (
    "Key Information + Executive Summary",
    "S1 公司概览（同 IM，无 ESOP 分析）",
    "S2 行业概览（简版）",
    "S3 核心 Thesis（简版）",
    "S4 风险与 Anti-thesis（简版）",
    "S5 Relevance to Universe（对 universe 内其他标的的多空含义，含跨区域）",
    "S6 估值（street 预期、框架、event pathway、IRR）",
    "S7 数据跟踪",
)


class ResearchQualityError(RuntimeError):
    """Base error for the quality loop."""


class ResearchQualityValidationError(ResearchQualityError):
    """A closed field or argument is invalid."""


class ResearchQualityConflict(ResearchQualityError):
    """An append-only record was reused with different semantics."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise ResearchQualityValidationError(
            f"{name} must be text of {minimum}..{maximum} characters"
        )
    return value.strip()


# ---------------------------------------------------------------------------
# the artefact shape
#
# One shape for three artefacts, so a check is written once.  An ask answer is
# a one-section document whose "numbers" are the Claims it cited: that is
# exactly what the number check needs, and it means "the answer invented a
# figure" and "the screen invented a figure" are the same question.
# ---------------------------------------------------------------------------


def _section(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": str(value.get("title") or ""),
        "body": str(value.get("body") or ""),
        "claim_refs": [str(ref) for ref in (value.get("claim_refs") or [])],
        "numbers": [
            {
                "text": str(item.get("text") or ""),
                "claim_version_ref": str(item.get("claim_version_ref") or ""),
                "period": item.get("period"),
            }
            for item in (value.get("numbers") or [])
        ],
        "gaps": [str(gap) for gap in (value.get("gaps") or [])],
    }


def artefact(
    *,
    artefact_kind: str,
    ref: str,
    hash: str,
    sections: Sequence[Mapping[str, Any]],
    title: str = "",
    subject_ref: str | None = None,
    gaps: Sequence[str] = (),
    expected_sections: Sequence[str] | None = None,
    shown_claims: Sequence[Mapping[str, Any]] = (),
    cited_tags: Sequence[str] = (),
    confidence: str | None = None,
    question: str | None = None,
    prior: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The common shape every check reads."""

    if artefact_kind not in ARTEFACT_KINDS:
        raise ResearchQualityValidationError(
            f"artefact_kind must be one of {list(ARTEFACT_KINDS)}"
        )
    return {
        "artefact_kind": artefact_kind,
        "ref": _text(ref, "ref", maximum=512),
        "hash": _text(hash, "hash", maximum=128),
        "title": title,
        "subject_ref": subject_ref,
        "sections": [_section(item) for item in sections],
        "gaps": [str(gap) for gap in gaps],
        "expected_sections": None if expected_sections is None else [str(t) for t in expected_sections],
        "shown_claims": [dict(claim) for claim in shown_claims],
        "cited_tags": [str(tag) for tag in cited_tags],
        "confidence": confidence,
        "question": question,
        "prior": None if prior is None else {
            "sections": [_section(item) for item in (prior.get("sections") or [])],
        },
    }


def artefact_from_deliverable(
    record: Mapping[str, Any], *, playbook: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A published ``mission_deliverable`` version as a scoreable artefact."""

    expected: Sequence[str] | None = INITIAL_SCREEN_SECTIONS
    kind = str(record.get("kind") or "initial_screen")
    if playbook is not None:
        titles = (playbook.get("deliverable_templates") or {}).get(kind)
        if isinstance(titles, list) and titles:
            expected = [str(title) for title in titles]
    return artefact(
        artefact_kind="initial_screen",
        ref=str(record["id"]),
        hash=str(record["content_hash"]),
        title=str(record.get("deliverable_ref") or ""),
        subject_ref=record.get("subject_ref"),
        sections=record.get("sections") or (),
        gaps=record.get("gaps") or (),
        expected_sections=expected,
    )


def artefact_from_ask_answer(
    result: Mapping[str, Any],
    *,
    shown_claims: Sequence[Mapping[str, Any]],
    ref: str,
    hash: str | None = None,
) -> dict[str, Any]:
    """A cockpit answer as a scoreable artefact.

    The answer is a cockpit artifact and has no Core record, so it has no
    content hash of its own; one is computed over what was said and what it
    cited, which is what a score has to be bound to anyway.
    """

    answer = str(result.get("answer") or "")
    citations = list(result.get("citations") or [])
    cited_tags = [str(item.get("tag") or item) for item in citations]
    by_tag = {str(claim.get("tag")): claim for claim in shown_claims}
    numbers = []
    claim_refs = []
    for tag in cited_tags:
        claim = by_tag.get(tag)
        if claim is None:
            continue
        claim_refs.append(str(claim.get("ref") or ""))
        numbers.append({
            "text": str(claim.get("statement") or ""),
            "claim_version_ref": str(claim.get("ref") or ""),
            "period": claim.get("period"),
        })
    digest = hash or content_hash({
        "question": result.get("question"), "answer": answer,
        "citations": cited_tags, "confidence": result.get("confidence"),
    })
    return artefact(
        artefact_kind="ask_answer",
        ref=ref,
        hash=digest,
        title=str(result.get("question") or "")[:200],
        question=result.get("question"),
        sections=[{
            "title": "答案", "body": answer, "claim_refs": claim_refs,
            "numbers": numbers, "gaps": result.get("gaps") or [],
        }],
        gaps=result.get("gaps") or (),
        shown_claims=shown_claims,
        cited_tags=cited_tags,
        confidence=result.get("confidence"),
        expected_sections=["答案"],
    )


def artefact_from_dossier(
    record: Mapping[str, Any], *, prior: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """A ``CompanyDossierVersion`` (Wave 2) as a scoreable artefact."""

    return artefact(
        artefact_kind="company_dossier",
        ref=str(record["id"]),
        hash=str(record["content_hash"]),
        title=str(record.get("dossier_ref") or ""),
        subject_ref=record.get("subject_ref"),
        sections=record.get("sections") or (),
        gaps=record.get("gaps") or (),
        expected_sections=DOSSIER_SECTIONS,
        prior=None if prior is None else {"sections": prior.get("sections") or []},
    )


# Q2: what a weekly brief prints that is machinery rather than prose.
#
# The rendered brief is a document with a Ledger inside it: every claim, every
# evidence version, every content hash and every CIK is printed inline, and a
# figure check run over that raw text would report a CIK as an untraceable
# number and a content hash as a figure.  So the adapter lifts the machinery
# out into ``claim_refs`` -- where a citation belongs -- and leaves a visible
# 〔ref〕 in its place.
#
# The marker is not decoration.  Stripping refs silently would hide exactly the
# thing ``citation_hygiene`` grades on a brief: whether the machine's addresses
# are carried by the citation fields or smeared through the prose a person has
# to read.  A judge counting 〔ref〕 per sentence is reading the real defect.
_BRIEF_CLAIM_REF_RE = re.compile(r"claim-version:[0-9a-f]{64}")
_BRIEF_COMPANY_REF_RE = re.compile(r"company:[a-z0-9-]+:[0-9]+")
_BRIEF_MACHINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-z][a-z0-9-]*-version:[0-9a-zA-Z:._-]+"),
    re.compile(r"(?:hash|snapshot_hash|content_hash)=[0-9a-f]{16,}"),
    re.compile(r"(?:retrieved|delivered)=\d{4}-\d{2}-\d{2}T[0-9:.+-]+"),
    re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"),
    re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{32,}(?![0-9a-zA-Z])"),
)
REF_MARKER = "〔ref〕"
_BRIEF_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _brief_prose(text: str, company_names: Mapping[str, str]) -> str:
    """One section's prose with machine addresses replaced by a marker."""

    def mark(match: "re.Match[str]") -> str:
        return REF_MARKER

    def name(match: "re.Match[str]") -> str:
        return company_names.get(match.group(0)) or REF_MARKER

    body = _BRIEF_CLAIM_REF_RE.sub(mark, text)
    body = _BRIEF_COMPANY_REF_RE.sub(name, body)
    for pattern in _BRIEF_MACHINE_PATTERNS:
        body = pattern.sub(mark, body)
    # A run of markers left by "｜ref｜hash｜retrieved" is one marker: the
    # density that matters is per sentence, not per field.
    body = re.sub(f"(?:{re.escape(REF_MARKER)}[\\s｜|,，、]*)+", REF_MARKER + " ", body)
    return body.strip()


def artefact_from_weekly_brief(
    issue: Mapping[str, Any],
    *,
    body: str,
    claim_versions: Sequence[Mapping[str, Any]] = (),
    company_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """A published ``WeeklyBriefIssueVersion`` as a scoreable artefact.

    ``body`` is the rendered markdown -- ``WeeklyBriefAuthority.render_markdown``
    replays it from the evidence pack, so it is the document as published and
    not a copy that can drift.  ``claim_versions`` are the snapshot's Claims,
    which is where a printed figure has to be traceable to.

    Each cited Claim contributes two source strings: its
    ``normalized_statement`` (where the figure came from) and the ``value unit``
    rendering the brief actually prints.  Both are needed because they are not
    the same token -- the filing says ``up 5.59%`` and the brief prints
    ``5.59 percent`` -- and a number check that only knew the first would report
    every figure in every brief as unsourced.
    """

    names = dict(company_names or {})
    claims = {str(item.get("claim_version_ref") or item.get("id") or ""): item
              for item in claim_versions}
    matches = list(_BRIEF_SECTION_RE.finditer(body or ""))
    sections: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        raw = body[match.end():end]
        refs = list(dict.fromkeys(_BRIEF_CLAIM_REF_RE.findall(raw)))
        prose = _brief_prose(raw, names)
        numbers: list[dict[str, Any]] = []
        for ref in refs:
            claim = claims.get(ref)
            if claim is None:
                continue
            period = claim.get("period")
            statement = str(claim.get("normalized_statement") or "")
            if statement:
                numbers.append({"text": statement, "claim_version_ref": ref, "period": period})
            value = claim.get("value")
            if value is not None:
                rendered = f"{value} {claim.get('unit') or ''}".strip()
                numbers.append({"text": rendered, "claim_version_ref": ref, "period": period})
        sections.append({
            "title": title, "body": prose, "claim_refs": refs, "numbers": numbers,
            "gaps": [],
        })
    return artefact(
        artefact_kind="weekly_brief",
        ref=str(issue["id"]),
        hash=str(issue["content_hash"]),
        title=str(issue.get("brief_ref") or ""),
        subject_ref=issue.get("industry_ref"),
        sections=sections,
        gaps=issue.get("gaps") or (),
        expected_sections=WEEKLY_BRIEF_SECTIONS,
    )


# ---------------------------------------------------------------------------
# the deterministic layer
# ---------------------------------------------------------------------------

# The citation scaffolding the drafting prompt introduces, and the wreckage its
# removal leaves.  These are the exact shapes that reached published screens.
_BARE_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])")
_SEPARATORS = "、,，/;；·"
_SENTENCE_END = "。；！？：:!?"
_SEPARATOR_RUN_RE = re.compile(f"[{_SEPARATORS}]{{2,}}")
_SEPARATOR_AFTER_END_RE = re.compile(f"[{_SENTENCE_END}][{_SEPARATORS}]")
_SEPARATOR_BEFORE_CLOSE_RE = re.compile(f"[{_SEPARATORS}]\\s*[)）\\]]")
_LEADING_SEPARATOR_RE = re.compile(f"(?:^|\n)\\s*[{_SEPARATORS}]")
_BRACKET_RE = re.compile(r"[（(\[]([^（()）\[\]]*)[)）\]]")
# A sentence whose subject went with the tag.  Only the shapes that are
# ungrammatical on their own: a conjunction immediately followed by a reporting
# verb ("，和指出…"), or a reporting verb starting a sentence ("：显示…").
# "，表示" is ordinary Chinese and is deliberately not here.
_ORPHAN_VERBS = "显示|指出|提到|表示|强调|披露|认为|反映|则反映|则"
_ORPHAN_CONJUNCTION_RE = re.compile(f"(?:和|与|及)(?:{_ORPHAN_VERBS})")
# A bracketed run may sit between the sentence boundary and the verb -- live,
# "；、、（同一季度数据重复）显示2025-…" is exactly that, and once the "、、" run
# is stripped the parenthetical is all that stands where the subject was.
_ORPHAN_SENTENCE_START_RE = re.compile(
    f"[{_SENTENCE_END}]\\s*(?:[（(][^（()）]*[)）]\\s*)?(?:{_ORPHAN_VERBS})"
)
_PARAGRAPH_RE = re.compile(r"\n+")
_SENTENCE_SPLIT_RE = re.compile(f"[{_SENTENCE_END}]")

_ARTEFACT_CODES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("leftover_tag", _BARE_TAG_RE),
    ("separator_run", _SEPARATOR_RUN_RE),
    ("separator_after_sentence_end", _SEPARATOR_AFTER_END_RE),
    ("separator_before_closing_bracket", _SEPARATOR_BEFORE_CLOSE_RE),
    ("leading_separator", _LEADING_SEPARATOR_RE),
    ("orphan_conjunction_verb", _ORPHAN_CONJUNCTION_RE),
    ("orphan_sentence_start_verb", _ORPHAN_SENTENCE_START_RE),
)


def _has_content(text: str) -> bool:
    return any(char.isalnum() for char in text)


def _excerpt(body: str, start: int, end: int, *, width: int = 18) -> str:
    return body[max(0, start - width):min(len(body), end + width)].replace("\n", " ")


def residual_citation_artefacts(body: str) -> list[dict[str, Any]]:
    """Everything the tag stripper leaves behind, with the offending excerpt.

    Each finding carries ``at``, the offset it was found at, because a caller
    that gates on these has to be able to ask where -- punctuation inside a URL
    is punctuation inside a URL, and "https://" is a colon and two slashes.
    Scoring does not need to ask; ``initial_screen_cli`` does.
    """

    findings: list[dict[str, Any]] = []
    for code, pattern in _ARTEFACT_CODES:
        for match in pattern.finditer(body or ""):
            findings.append({
                "code": code, "matched": match.group(0).strip(), "at": match.start(),
                "excerpt": _excerpt(body, match.start(), match.end()),
            })
    for match in _BRACKET_RE.finditer(body or ""):
        inner = match.group(1)
        empty = not _has_content(inner)
        if not empty:
            for colon in ("：", ":"):
                if colon in inner and not _has_content(inner.rsplit(colon, 1)[1]):
                    empty = True
                    break
        if empty:
            findings.append({
                "code": "empty_parenthetical", "matched": match.group(0), "at": match.start(),
                "excerpt": _excerpt(body, match.start(), match.end()),
            })
    return findings


def _figure_key(item: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """What a cited figure asserts: its period and its digits, not its wording."""

    tokens = tuple(sorted(re.sub(r"[\s,]", "", token) for token in value_tokens(str(item.get("text") or ""))))
    return (str(item.get("period") or ""), tokens)


CheckContext = Mapping[str, Any]


def _result(check: str, *, status: str, findings: Sequence[Mapping[str, Any]] = (), detail: str = "") -> dict[str, Any]:
    return {
        "check": check, "status": status, "count": len(findings),
        "findings": [dict(item) for item in findings][:40], "detail": detail,
    }


def check_numbers_without_refs(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    findings = []
    for section in art["sections"]:
        for token in unsourced_numbers(section["body"], section["numbers"]):
            findings.append({"section": section["title"], "figure": token})
    return _result(
        "numbers_without_refs",
        status="fail" if findings else "pass",
        findings=findings,
        detail=("每个数字都由本节引用的定量来源承载" if not findings
                else f"{len(findings)} 个数字在正文里出现但没有任何被引来源承载它"),
    )


def check_residual_citation_artefacts(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    findings = []
    for section in art["sections"]:
        for finding in residual_citation_artefacts(section["body"]):
            findings.append({"section": section["title"], **finding})
    return _result(
        "residual_citation_artefacts",
        status="fail" if findings else "pass",
        findings=findings,
        detail=("正文中没有引用脚手架的残迹" if not findings
                else f"{len(findings)} 处引用标记剥离后的残迹"),
    )


def check_duplicate_parallel_citations(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    """The same fact cited more than once, side by side.

    Live: three Claims asserted Accenture's 2025Q1 revenue in identical words,
    so S7 listed three refs for one figure and the prose said so.
    """

    findings = []
    for section in art["sections"]:
        groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for item in section["numbers"]:
            key = _figure_key(item)
            if not key[1]:
                continue
            refs = groups.setdefault(key, [])
            if item["claim_version_ref"] not in refs:
                refs.append(item["claim_version_ref"])
        for (period, tokens), refs in groups.items():
            if len(refs) > 1:
                findings.append({
                    "section": section["title"], "code": "duplicate_number_entries",
                    "period": period, "figures": list(tokens), "refs": refs,
                })
        # And the same figure written twice inside one sentence, which is what
        # a reader sees when the context offered it twice.
        for sentence in _SENTENCE_SPLIT_RE.split(section["body"]):
            counts: dict[str, int] = {}
            for token in value_tokens(sentence):
                normalised = re.sub(r"[\s,]", "", token)
                counts[normalised] = counts.get(normalised, 0) + 1
            for figure, count in counts.items():
                if count > 1:
                    findings.append({
                        "section": section["title"], "code": "figure_repeated_in_sentence",
                        "figure": figure, "times": count,
                        "excerpt": sentence.strip()[:120],
                    })
    return _result(
        "duplicate_parallel_citations",
        status="fail" if findings else "pass",
        findings=findings,
        detail=("同一个事实只被引用一次" if not findings
                else f"{len(findings)} 处同一事实的并列引用"),
    )


def check_required_sections_present(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    expected = art.get("expected_sections")
    if not expected:
        return _result("required_sections_present", status="skipped",
                       detail="没有给出模板章节，无法比对")
    written = [section["title"] for section in art["sections"]]
    findings = []
    for title in expected:
        if title not in written:
            findings.append({"code": "missing_section", "title": title})
    for section in art["sections"]:
        if not section["body"].strip() and not section["gaps"]:
            findings.append({"code": "empty_shell", "title": section["title"]})
    return _result(
        "required_sections_present",
        status="fail" if findings else "pass",
        findings=findings,
        detail=(f"{len(expected)} 节齐备，没有空壳" if not findings
                else f"{len(findings)} 个章节缺失或是空壳"),
    )


def check_claim_refs_resolve(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    """Every ref the artefact cites is a live claim version in the Core."""

    core = context.get("core")
    if core is None:
        return _result("claim_refs_resolve", status="skipped",
                       detail="没有 Core 连接，引用无法解析（golden 集与离线评分下为正常）")
    refs: list[str] = []
    for section in art["sections"]:
        refs.extend(section["claim_refs"])
        refs.extend(item["claim_version_ref"] for item in section["numbers"])
    unique = [ref for ref in dict.fromkeys(refs) if ref]
    if not unique:
        return _result("claim_refs_resolve", status="fail",
                       findings=[{"code": "no_citations"}],
                       detail="整份产物没有任何引用")
    known = set()
    for chunk in range(0, len(unique), 200):
        window = unique[chunk:chunk + 200]
        rows = core.execute(
            "SELECT claim_version_id FROM claim_versions WHERE claim_version_id IN "
            f"({','.join('?' * len(window))})", window,
        ).fetchall()
        known.update(row[0] for row in rows)
    retired: set[str] = set()
    try:
        retired = {
            row[0] for row in core.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
            ).fetchall()
        }
    except sqlite3.OperationalError as exc:  # the table is optional
        if "no such table" not in str(exc):
            raise
    findings = []
    for ref in unique:
        if ref not in known:
            findings.append({"code": "unknown_claim", "ref": ref})
        elif ref in retired:
            findings.append({"code": "retired_claim", "ref": ref})
    return _result(
        "claim_refs_resolve",
        status="fail" if findings else "pass",
        findings=findings,
        detail=(f"{len(unique)} 条引用全部在账本中且未撤回" if not findings
                else f"{len(findings)} 条引用无法解析或已撤回"),
    )


def check_cites_only_shown_claims(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    shown = {str(claim.get("tag")) for claim in art["shown_claims"]}
    if not shown:
        return _result("cites_only_shown_claims", status="skipped",
                       detail="没有记录这次展示了哪些 Claim")
    findings = [
        {"code": "unshown_tag", "tag": tag}
        for tag in art["cited_tags"] if tag not in shown
    ]
    return _result(
        "cites_only_shown_claims",
        status="fail" if findings else "pass",
        findings=findings,
        detail=(f"{len(art['cited_tags'])} 条引用全部来自这次展示的 {len(shown)} 条 Claim"
                if not findings else f"{len(findings)} 条引用不在展示范围内"),
    )


def check_confidence_stated(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    confidence = art.get("confidence")
    if confidence in {"high", "medium", "low"}:
        return _result("confidence_stated", status="pass", detail=f"信心：{confidence}")
    return _result(
        "confidence_stated", status="fail",
        findings=[{"code": "missing_confidence", "value": confidence}],
        detail="没有给出 high / medium / low 之一的信心",
    )


def check_every_section_cites(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    findings = [
        {"code": "section_without_citation", "title": section["title"]}
        for section in art["sections"]
        if section["body"].strip() and not section["claim_refs"] and not section["numbers"]
    ]
    return _result(
        "every_section_cites",
        status="fail" if findings else "pass",
        findings=findings,
        detail=("每个写出正文的章节都有引用" if not findings
                else f"{len(findings)} 个章节写了正文却没有引用"),
    )


def check_new_version_cites_new_refs(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    prior = art.get("prior")
    if not prior:
        return _result("new_version_cites_new_refs", status="skipped",
                       detail="这是第一版，没有上一版可比")
    def refs(sections: Sequence[Mapping[str, Any]]) -> set[str]:
        out: set[str] = set()
        for section in sections:
            out.update(section["claim_refs"])
            out.update(item["claim_version_ref"] for item in section["numbers"])
        return {ref for ref in out if ref}
    fresh = refs(art["sections"]) - refs(prior["sections"])
    if fresh:
        return _result("new_version_cites_new_refs", status="pass",
                       detail=f"这一版引用了 {len(fresh)} 条上一版没有引用过的证据")
    return _result(
        "new_version_cites_new_refs", status="fail",
        findings=[{"code": "no_new_evidence"}],
        detail="这一版没有引用任何新证据：按蓝图的止损规则，它应当是 duplicate 而不是新版本",
    )


def check_restatement_drift(art: Mapping[str, Any], context: CheckContext) -> dict[str, Any]:
    prior = art.get("prior")
    if not prior:
        return _result("restatement_drift", status="skipped",
                       detail="这是第一版，没有上一版可比")
    before = {section["title"]: section["body"] for section in prior["sections"]}
    findings = []
    compared = 0
    advanced = 0
    for section in art["sections"]:
        old = before.get(section["title"])
        if not old or not section["body"].strip():
            continue
        compared += 1
        ratio = difflib.SequenceMatcher(None, old, section["body"]).ratio()
        if ratio >= RESTATEMENT_SIMILARITY:
            findings.append({
                "code": "restated_section", "title": section["title"],
                "similarity": round(ratio, 3),
            })
        else:
            advanced += 1
    if not compared:
        return _result("restatement_drift", status="skipped",
                       detail="没有一节可以与上一版逐节比对")
    # A version that revisits one section and leaves the rest alone is a
    # normal version, not drift.  Drift is a version where *nothing* moved:
    # every section rewritten into the same thing.  Judging section by section
    # would make honest, narrow revisions the most-punished kind.
    return _result(
        "restatement_drift",
        status="fail" if advanced == 0 else "pass",
        findings=findings,
        detail=(f"{compared} 节里有 {advanced} 节相对上一版有实质改动"
                if advanced else
                f"{compared} 节全部是上一版的同义改写（重合度均不低于 {RESTATEMENT_SIMILARITY}）"),
    )



# ---------------------------------------------------------------------------
# Q2: the capability gate
#
# Two of the six things the owner's weekly meeting asks for need a layer the
# system has not been granted.  A brief that does not attribute last week's
# price move is not a bad brief; it is a brief written by a system that is
# blind to price.  So the gate is a *deterministic* check and not a judge
# question: whether the market authority is granted is a fact about the Core,
# and a model asked to decide it would be guessing about its own installation.
#
# Absent unless proven present.  A score run without a Core connection -- the
# golden set, an offline rescore -- has no evidence that either capability
# exists, and defaulting to "present" would silently start grading documents
# against sections nobody can write.
# ---------------------------------------------------------------------------

# capability -> (table that would hold it, the may_write scope that admits it)
WEEKLY_BRIEF_CAPABILITY_SOURCES: Mapping[str, tuple[str, str]] = {
    "market_price": ("market_price_series_versions", "market_price"),
    "debate_map": ("debate_map_versions", "debate_map"),
}


def _mission_write_scopes(
    core: sqlite3.Connection, mission_ref: str | None = None
) -> set[str] | None:
    """What a mission is allowed to write, or None if unreadable.

    Scoped to one mission when the caller names one.  A union across every
    mission on the Core would let a second mission's grant open a gate for the
    document of a mission that does not have it -- there is one mission today,
    which is exactly when this kind of thing gets written and never noticed.
    """

    sql = (
        "SELECT v.record_json AS record_json, p.mission_ref AS mission_ref "
        "FROM coverage_mission_pointer p "
        "JOIN coverage_mission_versions v ON v.mission_version_id=p.mission_version_id"
    )
    params: tuple[Any, ...] = ()
    if mission_ref:
        sql += " WHERE p.mission_ref=?"
        params = (mission_ref,)
    try:
        rows = core.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return None
    scopes: set[str] = set()
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError):
            continue
        autonomy = record.get("autonomy") or {}
        scopes.update(str(scope) for scope in (autonomy.get("may_write") or []))
    return scopes


def weekly_brief_capabilities(
    core: sqlite3.Connection | None, *, mission_ref: str | None = None,
) -> dict[str, dict[str, Any]]:
    """For each gated capability: is it there, and if not, why not.

    A capability counts as present only when both halves are true -- the
    authority holds at least one record, and a live mission grants the write
    scope that admits it.  Either half alone is a half-installed layer: rows
    nobody may write about, or a permission with nothing behind it.
    """

    if core is None:
        return {
            name: {"present": False,
                   "reason": "没有 Core 连接，无法确认这一层是否已接入；按未接入处理"}
            for name in WEEKLY_BRIEF_CAPABILITY_SOURCES
        }
    scopes = _mission_write_scopes(core, mission_ref)
    out: dict[str, dict[str, Any]] = {}
    for name, (table, scope) in WEEKLY_BRIEF_CAPABILITY_SOURCES.items():
        try:
            rows = core.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            rows = None
            present_table = False
        else:
            present_table = rows is not None
        granted = scopes is not None and scope in scopes
        if present_table and granted:
            out[name] = {"present": True, "reason": f"{table} 有记录，且 mission 已授予 {scope}"}
        elif not present_table:
            out[name] = {"present": False,
                         "reason": f"{table} 尚无记录：这一层还没有产出任何版本"}
        else:
            out[name] = {"present": False,
                         "reason": f"live mission 的 may_write 未授予 {scope}"}
    return out


def check_weekly_brief_capability_gate(
    art: Mapping[str, Any], context: CheckContext
) -> dict[str, Any]:
    """Which criteria this Core cannot yet hold against a weekly brief.

    Reported as ``skipped`` rather than ``fail``: nothing about the document
    failed.  ``count`` is the number of criteria withheld, so the golden set
    pins how much of the rubric is currently unusable -- which is the number
    that should fall to zero as Wave 1A and Wave 2 land.
    """

    rubric: Rubric | None = context.get("rubric")
    capabilities = weekly_brief_capabilities(
        context.get("core"), mission_ref=context.get("mission_ref"),
    )
    findings: list[dict[str, Any]] = []
    criteria = () if rubric is None else rubric.criteria
    for criterion in criteria:
        name = criterion.capability
        if name is None:
            continue
        state = capabilities.get(name)
        if state is None or state["present"]:
            continue
        findings.append({
            "code": "not_applicable_yet",
            "criterion_id": criterion.criterion_id,
            "capability": name,
            "reason": state["reason"],
        })
    if not findings:
        return _result(
            "weekly_brief_capability_gate", status="pass",
            detail="这份评分表的每条标准今天都可评",
        )
    return _result(
        "weekly_brief_capability_gate", status="skipped", findings=findings,
        detail=(f"{len(findings)} 条标准所依赖的能力尚未接入，按 not_applicable_yet 发布："
                + "；".join(f"{item['criterion_id']}（{item['reason']}）" for item in findings)),
    )


CHECKS: Mapping[str, Callable[[Mapping[str, Any], CheckContext], dict[str, Any]]] = {
    "numbers_without_refs": check_numbers_without_refs,
    "residual_citation_artefacts": check_residual_citation_artefacts,
    "duplicate_parallel_citations": check_duplicate_parallel_citations,
    "required_sections_present": check_required_sections_present,
    "claim_refs_resolve": check_claim_refs_resolve,
    "cites_only_shown_claims": check_cites_only_shown_claims,
    "confidence_stated": check_confidence_stated,
    "every_section_cites": check_every_section_cites,
    "new_version_cites_new_refs": check_new_version_cites_new_refs,
    "restatement_drift": check_restatement_drift,
    "weekly_brief_capability_gate": check_weekly_brief_capability_gate,
}


def withheld_criteria(deterministic: Mapping[str, Any] | None) -> dict[str, str]:
    """Criteria the capability gate withheld, and why.

    Q2 review: a withheld criterion is not a low score, and everything
    downstream has to agree about that or it becomes one.  The judge is told
    not to score them, ``summarise_scores`` keeps them out of the mean and out
    of ``below_passing``, and the record publishes them as
    ``not_applicable_yet`` with the reason.  One reading of the gate, used by
    all three, so they cannot drift apart.
    """

    if not deterministic:
        return {}
    for item in deterministic.get("checks") or ():
        if item.get("check") != "weekly_brief_capability_gate":
            continue
        return {
            str(finding["criterion_id"]): str(finding.get("reason") or "")
            for finding in item.get("findings") or ()
            if finding.get("code") == "not_applicable_yet"
        }
    return {}


def run_deterministic(
    art: Mapping[str, Any], rubric: Rubric, *,
    core: sqlite3.Connection | None = None, mission_ref: str | None = None,
) -> dict[str, Any]:
    """Every check this rubric names, in the rubric's own order."""

    # The rubric travels in the context because Q2's capability gate is a
    # check about the *rubric*: which of its criteria this Core cannot yet
    # hold against a document. Every other check ignores it.
    context: CheckContext = {"core": core, "rubric": rubric, "mission_ref": mission_ref}
    results = [CHECKS[name](art, context) for name in rubric.deterministic_checks]
    failed = [item["check"] for item in results if item["status"] == "fail"]
    return {
        "scorer_version": SCORER_VERSION,
        "rubric_ref": rubric.rubric_ref,
        "rubric_hash": rubric.content_hash,
        "target_ref": art["ref"],
        "target_hash": art["hash"],
        "checks": results,
        "failed_checks": failed,
        "passed": not failed,
    }


# ---------------------------------------------------------------------------
# the judge layer
# ---------------------------------------------------------------------------


def build_judge_prompt(art: Mapping[str, Any], rubric: Rubric, deterministic: Mapping[str, Any]) -> str:
    """The rubric, the artefact, its cited Claims, and the checks already done."""

    lines = [
        "You are grading one research artefact against a fixed rubric for a fundamental",
        "long-biased hedge fund's own file. You are not rewriting it and not advising on it.",
        "",
        f"Rubric: {rubric.title} ({rubric.rubric_ref} v{rubric.version})",
        f"What it is for: {rubric.intent}",
        "",
        "Scale (the same for every criterion):",
    ]
    lines.extend(f"  {level} = {label}" for level, label in sorted(SCALE.items()))
    if rubric.grading_notes:
        lines.append("")
        lines.append("Grading notes:")
        lines.extend(f"  - {note}" for note in rubric.grading_notes)
    withheld = withheld_criteria(deterministic)
    lines.append("")
    lines.append("Criteria:")
    for criterion in rubric.criteria:
        if criterion.criterion_id in withheld:
            lines.append(
                f"- {criterion.criterion_id}: NOT APPLICABLE YET — do not score this one. "
                f"{withheld[criterion.criterion_id]}"
            )
            continue
        lines.append(f"- {criterion.criterion_id}: {criterion.question}")
        lines.append(f"    评分依据必须能指认：{criterion.evidence_required}")
        for level in ("0", "2", "4"):
            lines.append(f"    {level} = {criterion.anchors[level]}")
    if withheld:
        lines.append("")
        lines.append(
            "The criteria marked NOT APPLICABLE YET need a system capability that is not "
            "installed. Return no entry for them, or an entry whose score is null. A number "
            "for one of them is refused: the artefact did not fail at something nothing could "
            "have done."
        )
    lines.append("")
    lines.append("Checks already run mechanically (do not re-derive them; use them as facts):")
    for item in deterministic["checks"]:
        lines.append(f"- {item['check']}: {item['status']} — {item['detail']}")
    lines.append("")
    lines.append("Return raw JSON only, no markdown fence, with this exact shape and nothing else:")
    lines.append('{"scores": [{"criterion_id": "<one of the ids above>", "score": 0-4,')
    lines.append('             "evidence": "<one sentence naming what in the artefact you scored>"}]}')
    lines.append("One entry per criterion you were asked to score, exactly once, no other keys.")
    lines.append("The evidence sentence must point at the artefact, not restate the criterion.")
    lines.append("")
    if art.get("question"):
        lines.append(f"Question that was asked: {art['question']}")
    lines.append(f"Artefact ({art['artefact_kind']}): {art.get('title') or art['ref']}")
    budget = MAX_ARTEFACT_CHARS
    for section in art["sections"]:
        lines.append("")
        lines.append(f"## {section['title']}")
        body = section["body"]
        if len(body) > budget:
            body = body[:max(0, budget)] + "…（超出评分 prompt 的长度上限，已截断）"
        budget -= len(body)
        lines.append(body if body else "（本节没有正文）")
        if section["gaps"]:
            lines.append("gaps: " + " | ".join(section["gaps"])[:1200])
        if budget <= 0:
            lines.append("（其余章节因长度上限未展示）")
            break
    cited: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for section in art["sections"]:
        for item in section["numbers"]:
            ref = item["claim_version_ref"]
            if ref and ref not in seen:
                seen.add(ref)
                cited.append({"ref": ref, "period": item.get("period"), "text": item["text"]})
    for claim in art["shown_claims"]:
        ref = str(claim.get("ref") or "")
        if ref and ref not in seen:
            seen.add(ref)
            cited.append({"ref": ref, "period": claim.get("period"),
                          "text": str(claim.get("statement") or "")})
    if cited:
        lines.append("")
        lines.append(f"Cited evidence ({min(len(cited), MAX_CLAIM_ROWS)} of {len(cited)}), one row per Claim:")
        lines.append("| ref | period | statement |")
        for item in cited[:MAX_CLAIM_ROWS]:
            statement = str(item["text"]).replace("\n", " ")[:MAX_CLAIM_TEXT]
            lines.append(f"| {item['ref'][-16:]} | {item.get('period') or ''} | {statement} |")
    return "\n".join(lines)


def _one_sentence(value: Any, name: str) -> str:
    text = _text(value, name, maximum=MAX_EVIDENCE_CHARS)
    if "\n" in text:
        raise ResearchQualityValidationError(f"{name} must be one sentence")
    body = text.rstrip("".join("。！？.!?"))
    if any(char in body for char in "。！？"):
        raise ResearchQualityValidationError(f"{name} must be one sentence")
    return text


def validate_judge_output(
    value: Any, rubric: Rubric, *, withheld: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The judge's reply, or a refusal.  Nothing is repaired.

    A reply with an extra key, a criterion the rubric does not have, a missing
    criterion, a score out of range or an essay where a sentence belongs is not
    a judgement that needs tidying: it is a judgement about something other
    than this rubric, and it is refused.

    ``withheld`` names the criteria the capability gate took off the table.
    The judge may omit them or answer ``null``; **a number for one of them is
    refused**.  That refusal is the point of the whole gate: if a withheld
    criterion could come back as a 1, it would land in the mean and in
    ``below_passing`` and the document would be marked down for not doing a
    thing nothing could have done.
    """

    withheld = dict(withheld or {})

    if not isinstance(value, Mapping):
        raise ResearchQualityValidationError("the judge did not return an object")
    extra = set(value) - {"schema_version", "scores"}
    if extra:
        raise ResearchQualityValidationError(
            f"the judge returned keys the contract does not have: {sorted(extra)}"
        )
    rows = value.get("scores")
    if not isinstance(rows, list) or not rows:
        raise ResearchQualityValidationError("the judge returned no scores")
    scores: list[dict[str, Any]] = []
    declined: dict[str, str | None] = {}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - {"criterion_id", "score", "evidence"} or not {
            "criterion_id", "score"
        } <= set(row):
            raise ResearchQualityValidationError(
                "each score must be exactly criterion_id, score and evidence"
            )
        criterion_id = str(row["criterion_id"]).strip()
        if criterion_id not in rubric.criterion_ids:
            raise ResearchQualityValidationError(
                f"the judge scored a criterion this rubric does not have: {criterion_id!r}"
            )
        if criterion_id in seen:
            raise ResearchQualityValidationError(
                f"the judge scored {criterion_id!r} twice"
            )
        seen.add(criterion_id)
        score = row["score"]
        if criterion_id in withheld:
            if score is not None:
                raise ResearchQualityValidationError(
                    f"{criterion_id}: this criterion is not applicable yet and must not be "
                    f"scored; the judge returned {score!r}"
                )
            declined[criterion_id] = (
                None if row.get("evidence") is None
                else _one_sentence(row["evidence"], f"{criterion_id}.evidence")
            )
            continue
        if score is None:
            raise ResearchQualityValidationError(
                f"{criterion_id}: this criterion is applicable and must be scored"
            )
        if isinstance(score, bool) or not isinstance(score, int):
            raise ResearchQualityValidationError(
                f"{criterion_id}: score must be a whole number {SCORE_MIN}..{SCORE_MAX}"
            )
        if not SCORE_MIN <= score <= SCORE_MAX:
            raise ResearchQualityValidationError(
                f"{criterion_id}: score {score} is outside {SCORE_MIN}..{SCORE_MAX}"
            )
        if "evidence" not in row:
            raise ResearchQualityValidationError(
                f"{criterion_id}: a score needs one sentence of evidence"
            )
        scores.append({
            "criterion_id": criterion_id, "score": score,
            "evidence": _one_sentence(row["evidence"], f"{criterion_id}.evidence"),
        })
    missing = [
        item for item in rubric.criterion_ids
        if item not in seen and item not in withheld
    ]
    if missing:
        raise ResearchQualityValidationError(
            f"the judge did not score every criterion; missing: {missing}"
        )
    scores.sort(key=lambda item: rubric.criterion_ids.index(item["criterion_id"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "scores": scores,
        # Published beside the scores rather than mixed into them: a reader who
        # sums the list must not be able to sum a not-applicable.
        "withheld": [
            {"criterion_id": criterion_id, "reason": reason,
             "evidence": declined.get(criterion_id)}
            for criterion_id, reason in sorted(withheld.items())
        ],
    }


def summarise_scores(
    scores: Sequence[Mapping[str, Any]],
    withheld: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The arithmetic, over the criteria that were actually graded.

    ``withheld`` never enters ``mean`` or ``below_passing``.  Averaging a
    not-applicable is how a gate that was built to protect a document ends up
    marking it down: a criterion nothing could have satisfied would drag the
    mean toward zero and appear in the list of findings, which is a zero
    wearing a different name.
    """

    values = [int(item["score"]) for item in scores]
    return {
        "criteria": len(values),
        "minimum": min(values) if values else None,
        "mean": round(sum(values) / len(values), 3) if values else None,
        "below_passing": [
            item["criterion_id"] for item in scores if int(item["score"]) < PASSING_SCORE
        ],
        "withheld": [
            {"criterion_id": str(item["criterion_id"]),
             "status": "not_applicable_yet",
             "reason": str(item.get("reason") or "")}
            for item in withheld
        ],
    }


# A score with no judge layer was decided by code alone, and code has no model
# configuration.  Named rather than left as an empty string so a reader of the
# row can tell "no model was asked" from "we did not record which".
DETERMINISTIC_ONLY_FINGERPRINT = "none"


def judge_fingerprint(judge_layer: Mapping[str, Any] | None) -> str:
    """What "the same model configuration" means for a score's identity.

    Derived from the route decision the call actually ran under, not from
    anything the caller declares.  A caller-declared fingerprint is a caller
    -declared identity, and an identity a caller controls is not a duplicate
    rule -- it is a way around one.

    The route decision is stable across a genuine repeat: a cockpit call is
    content-addressed on (purpose, request_id, mission version, prompt), so
    asking the same question again replays the same result with the same route.
    A different profile is a different route and therefore a different score.

    The *outcome* is deliberately not part of it.  A model that answered and a
    model that answered badly are the same model, so a refusal and the
    judgement that replaces it share an identity -- which is what makes the
    replacement a new version of the same score rather than an unrelated one
    (see ``record``).  A refused judge that never reached a model has no route
    to name; it still gets a hash, because the identity column holds one shape.
    """

    if judge_layer is None:
        return DETERMINISTIC_ONLY_FINGERPRINT
    model = judge_layer.get("model") or {}
    return content_hash({
        "route_decision_ref": model.get("route_decision_ref"),
        "purpose": model.get("purpose") or JUDGE_PURPOSE,
    })


def judge(
    art: Mapping[str, Any],
    rubric: Rubric,
    deterministic: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """One bounded call, verified before it is believed."""

    prompt = build_judge_prompt(art, rubric, deterministic)
    withheld = withheld_criteria(deterministic)
    try:
        call = model.call(
            purpose=JUDGE_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission)
    except CockpitModelError as exc:
        return {
            # C2: refused either way -- the scoring path branches on
            # this word -- but a spent pool says so, so the run that
            # reports it can call it a budget decision rather than an
            # outage.
            "status": "refused", "reason": f"模型调用没有成功：{exc}",
            "lane_status": lane_status_for(exc, "refused"),
            "rubric_ref": rubric.rubric_ref, "rubric_hash": rubric.content_hash,
            "prompt_chars": len(prompt),
        }
    parsed = unwrap_json_object(call["text"])
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "result_envelope_ref": call.get("result_envelope_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": call.get("replayed"),
        "cost_usd": round((call.get("cost_micros") or 0) / 1_000_000, 6),
        "purpose": JUDGE_PURPOSE,
    }
    try:
        validated = validate_judge_output(parsed, rubric, withheld=withheld)
    except ResearchQualityValidationError as exc:
        return {
            "status": "refused", "reason": str(exc),
            "rubric_ref": rubric.rubric_ref, "rubric_hash": rubric.content_hash,
            "prompt_chars": len(prompt), "model": provenance,
        }
    return {
        "status": "scored",
        "rubric_ref": rubric.rubric_ref,
        "rubric_hash": rubric.content_hash,
        "scores": validated["scores"],
        "withheld": validated["withheld"],
        "summary": summarise_scores(validated["scores"], validated["withheld"]),
        "prompt_chars": len(prompt),
        "model": provenance,
    }


# ---------------------------------------------------------------------------
# the independent verifier (thesis_impact's pattern, one artefact smaller)
# ---------------------------------------------------------------------------

VERIFIER_VERDICTS: tuple[str, ...] = ("pass", "reject")
VERIFIER_FINDING_CODES: tuple[str, ...] = (
    "score_not_supported_by_evidence",
    "evidence_sentence_does_not_point_at_artefact",
    "criterion_misread",
    "deterministic_result_contradicted",
)


def build_verifier_prompt(art: Mapping[str, Any], rubric: Rubric, judgement: Mapping[str, Any]) -> str:
    """The verifier sees the artefact and the scores, and answers one question."""

    lines = [
        "You are an independent verifier. Another model graded a research artefact against a",
        "fixed rubric. You do not re-grade it and you do not improve it. You answer one",
        "question: is each score supported by the artefact and by the evidence sentence given?",
        "",
        f"Rubric: {rubric.title} ({rubric.rubric_ref} v{rubric.version})",
    ]
    # Criteria that were never scored are not shown: a verifier given an anchor
    # for a criterion with no score in front of it has been handed a missing
    # answer to find.
    graded = {str(row["criterion_id"]) for row in judgement.get("scores") or ()}
    for criterion in rubric.criteria:
        if graded and criterion.criterion_id not in graded:
            continue
        lines.append(f"- {criterion.criterion_id}: {criterion.question}")
        lines.append(f"    0 = {criterion.anchors['0']}")
        lines.append(f"    4 = {criterion.anchors['4']}")
    lines.append("")
    lines.append("Scores under review:")
    for row in judgement["scores"]:
        lines.append(f"- {row['criterion_id']} = {row['score']} — {row['evidence']}")
    lines.append("")
    lines.append("Return raw JSON only, nothing else:")
    lines.append('{"verdict": "pass|reject", "findings": [{"criterion_id": "<id>",')
    lines.append('   "code": "' + "|".join(VERIFIER_FINDING_CODES) + '", "detail": "<one sentence>"}]}')
    lines.append("A pass verdict must have no findings; a reject verdict must have at least one.")
    lines.append("")
    lines.append(f"Artefact ({art['artefact_kind']}): {art.get('title') or art['ref']}")
    budget = MAX_ARTEFACT_CHARS
    for section in art["sections"]:
        body = section["body"][:max(0, budget)]
        budget -= len(body)
        lines.append("")
        lines.append(f"## {section['title']}")
        lines.append(body if body else "（本节没有正文）")
        if budget <= 0:
            break
    return "\n".join(lines)


def validate_verifier_output(value: Any, rubric: Rubric) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchQualityValidationError("the verifier did not return an object")
    extra = set(value) - {"verdict", "findings"}
    if extra:
        raise ResearchQualityValidationError(
            f"the verifier returned keys the contract does not have: {sorted(extra)}"
        )
    verdict = value.get("verdict")
    if verdict not in VERIFIER_VERDICTS:
        raise ResearchQualityValidationError(f"invalid verdict: {verdict!r}")
    rows = value.get("findings") or []
    if not isinstance(rows, list):
        raise ResearchQualityValidationError("findings must be a list")
    findings = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"criterion_id", "code", "detail"}:
            raise ResearchQualityValidationError(
                "each finding must be exactly criterion_id, code and detail"
            )
        if str(row["criterion_id"]) not in rubric.criterion_ids:
            raise ResearchQualityValidationError(
                f"the verifier named a criterion this rubric does not have: {row['criterion_id']!r}"
            )
        if row["code"] not in VERIFIER_FINDING_CODES:
            raise ResearchQualityValidationError(f"unknown finding code: {row['code']!r}")
        findings.append({
            "criterion_id": str(row["criterion_id"]), "code": str(row["code"]),
            "detail": _one_sentence(row["detail"], "finding.detail"),
        })
    if verdict == "pass" and findings:
        raise ResearchQualityValidationError("a pass verdict must have no findings")
    if verdict == "reject" and not findings:
        raise ResearchQualityValidationError("a reject verdict must have at least one finding")
    return {"verdict": verdict, "findings": findings}


def verify(
    art: Mapping[str, Any],
    rubric: Rubric,
    judgement: Mapping[str, Any],
    *,
    model: Any,
    mission: Mapping[str, Any],
    request_id: str,
) -> dict[str, Any]:
    """A second, separate call that returns only a verdict on the first."""

    if judgement.get("status") != "scored":
        return {"status": "skipped", "reason": "没有可复核的评分"}
    prompt = build_verifier_prompt(art, rubric, judgement)
    try:
        call = independent_model_call(
            model,
            producer_route_decision_refs=[
                (judgement.get("model") or {}).get("route_decision_ref")],
            purpose=VERIFIER_PURPOSE, request_id=request_id, prompt=prompt,
            mission=mission,
        )
    except CockpitModelError as exc:
        return {"status": "refused", "reason": f"复核调用没有成功：{exc}",
                "lane_status": lane_status_for(exc, "refused")}
    provenance = {
        "work_order_ref": call.get("work_order_ref"),
        "invocation_ref": call.get("invocation_ref"),
        "route_decision_ref": call.get("route_decision_ref"),
        "replayed": call.get("replayed"),
        "cost_usd": round((call.get("cost_micros") or 0) / 1_000_000, 6),
        "purpose": VERIFIER_PURPOSE,
    }
    try:
        validated = validate_verifier_output(unwrap_json_object(call["text"]), rubric)
    except ResearchQualityValidationError as exc:
        return {"status": "refused", "reason": str(exc), "model": provenance}
    return {
        "status": "verified",
        # Bound to what was verified: a verdict that does not name the scores it
        # read is a verdict about nothing.
        "judged_scores_hash": content_hash(judgement["scores"]),
        **validated,
        "model": provenance,
    }


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringIdentity:
    """What makes two scores of the same document the same score."""

    target_ref: str
    target_hash: str
    rubric_ref: str
    rubric_hash: str
    scorer_version: str
    model_config_fingerprint: str

    def digest(self) -> str:
        return content_hash({
            "target_ref": self.target_ref, "target_hash": self.target_hash,
            "rubric_ref": self.rubric_ref, "rubric_hash": self.rubric_hash,
            "scorer_version": self.scorer_version,
            "model_config_fingerprint": self.model_config_fingerprint,
        })


class QualityScoreAuthority:
    """Append-only quality scores, one version chain per artefact and rubric."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_research_quality_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("QualityScoreAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads --------------------------------------------------------------

    def latest(self, score_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM research_quality_score_pointer p JOIN research_quality_score_versions v "
            "ON v.version_id=p.version_id WHERE p.score_ref=?",
            (_text(score_ref, "score_ref", maximum=512),),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise ResearchQualityConflict("quality score authority drifted")
        return record

    def scores_for(self, target_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM research_quality_score_versions WHERE target_ref=? "
            "ORDER BY created_at, version_id", (target_ref,),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def versions(self, score_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM research_quality_score_versions WHERE score_ref=? "
            "ORDER BY version_number", (score_ref,),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    # -- write --------------------------------------------------------------

    def record(
        self,
        *,
        artefact_kind: str,
        target_ref: str,
        target_hash: str,
        rubric: Rubric,
        deterministic: Mapping[str, Any],
        judge_layer: Mapping[str, Any] | None = None,
        verifier_layer: Mapping[str, Any] | None = None,
        subject_ref: str | None = None,
        actor_ref: str,
    ) -> dict[str, Any]:
        if artefact_kind not in ARTEFACT_KINDS:
            raise ResearchQualityValidationError(
                f"artefact_kind must be one of {list(ARTEFACT_KINDS)}"
            )
        target_ref = _text(target_ref, "target_ref", maximum=512)
        target_hash = _text(target_hash, "target_hash", maximum=128)
        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("human:") or actor_ref.startswith("automation:")):
            raise ResearchQualityValidationError(
                "actor_ref must be a human: or automation: principal"
            )
        if deterministic.get("rubric_hash") != rubric.content_hash:
            raise ResearchQualityConflict(
                "the deterministic layer was computed against a different rubric"
            )
        if deterministic.get("target_hash") != target_hash:
            raise ResearchQualityConflict(
                "the deterministic layer was computed against a different artefact"
            )
        if judge_layer is not None and judge_layer.get("rubric_hash") not in (None, rubric.content_hash):
            raise ResearchQualityConflict("the judge layer names a different rubric")
        judge_status = None if judge_layer is None else str(judge_layer.get("status"))
        if judge_status == "scored" and not (judge_layer.get("model") or {}).get("route_decision_ref"):
            raise ResearchQualityConflict(
                "a scored judge layer must name the route decision it ran under; "
                "without it the score cannot say which model answered"
            )
        # The verifier's verdict names the scores it read. If it names other
        # scores it is a verdict about another judgement, and storing the two
        # side by side would read as though it were about this one.
        if verifier_layer is not None and verifier_layer.get("status") == "verified":
            if judge_layer is None or not judge_layer.get("scores"):
                raise ResearchQualityConflict("a verifier verdict needs the judgement it verified")
            if verifier_layer.get("judged_scores_hash") != content_hash(judge_layer["scores"]):
                raise ResearchQualityConflict(
                    "the verifier verdict is bound to different scores than the judge layer"
                )
        fingerprint = judge_fingerprint(judge_layer)
        identity = ScoringIdentity(
            target_ref=target_ref, target_hash=target_hash,
            rubric_ref=rubric.rubric_ref, rubric_hash=rubric.content_hash,
            scorer_version=SCORER_VERSION,
            model_config_fingerprint=fingerprint,
        )
        digest = identity.digest()
        # Content-addressed rather than sliced off the tail of the ref: two
        # companies whose refs end in the same 64 characters are two documents,
        # and they were sharing a version chain.
        score_ref = (
            f"quality-score:{rubric.rubric_ref.split(':', 1)[-1]}:"
            f"{content_hash(target_ref)[:32]}"
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "score_ref": score_ref,
            "artefact_kind": artefact_kind,
            "target_ref": target_ref,
            "target_hash": target_hash,
            "subject_ref": subject_ref,
            "rubric_ref": rubric.rubric_ref,
            "rubric_version": rubric.version,
            "rubric_hash": rubric.content_hash,
            "scorer_version": SCORER_VERSION,
            "model_config_fingerprint": fingerprint,
            "scoring_identity_hash": digest,
            # The two layers stay apart on purpose: one is a fact about the
            # document, the other is a reading of it, and a reader has to be
            # able to trust the first without trusting the second.
            "deterministic": dict(deterministic),
            "judge": None if judge_layer is None else dict(judge_layer),
            "verifier": None if verifier_layer is None else dict(verifier_layer),
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        with self._transaction() as cur:
            seen = cur.execute(
                "SELECT version_id, judge_status FROM research_quality_score_versions "
                "WHERE score_ref=? AND scoring_identity_hash=? "
                "ORDER BY version_number DESC LIMIT 1", (score_ref, digest),
            ).fetchone()
            # A refusal does not settle an identity. The judge returned nothing
            # readable -- a malformed reply, a refused budget -- and treating
            # that as the answer would mean one bad reply permanently blocks
            # this document from ever being judged under this rubric. A refusal
            # can therefore be superseded by a real judgement, and by nothing
            # else: a second refusal is still a duplicate, so a retry loop
            # cannot fill the chain with them.
            settled = seen is not None and seen["judge_status"] in (None, "scored")
            if seen is not None and (settled or judge_status != "scored"):
                existing = cur.execute(
                    "SELECT record_json FROM research_quality_score_versions WHERE version_id=?",
                    (seen["version_id"],),
                ).fetchone()
                return {**json.loads(existing["record_json"]), "status": "duplicate"}
            pointer = cur.execute(
                "SELECT version_id, version_number FROM research_quality_score_pointer "
                "WHERE score_ref=?", (score_ref,),
            ).fetchone()
            version = 1 if pointer is None else int(pointer["version_number"]) + 1
            prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = (
                "quality-score-version:"
                + content_hash({"ref": score_ref, "version": version})[:32]
            )
            record["content_hash"] = content_hash(
                {k: v for k, v in record.items() if k != "content_hash"}
            )
            cur.execute(
                "INSERT INTO research_quality_score_versions(version_id,score_ref,version_number,"
                "prior_version_ref,artefact_kind,target_ref,target_hash,subject_ref,rubric_ref,"
                "rubric_hash,scorer_version,model_config_fingerprint,scoring_identity_hash,"
                "judge_status,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], score_ref, version, prior, artefact_kind, target_ref, target_hash,
                 subject_ref, rubric.rubric_ref, rubric.content_hash, SCORER_VERSION,
                 fingerprint, digest, judge_status,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO research_quality_score_pointer(score_ref,version_id,version_number,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (score_ref, record["id"], version, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE research_quality_score_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE score_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"], score_ref),
                )
        # Read back what was written rather than trusting what was sent.
        written = self.latest(score_ref)
        if written is None or written["id"] != record["id"]:
            raise ResearchQualityConflict("the quality score did not read back")
        return {**written, "status": "fresh"}


def score_artefact(
    art: Mapping[str, Any],
    rubric_name: str,
    *,
    core: sqlite3.Connection | None = None,
    model: Any = None,
    mission: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    verifier_model: Any = None,
) -> dict[str, Any]:
    """Run the deterministic layer always, the judge layer only if given a model."""

    rubric = get_rubric(rubric_name)
    deterministic = run_deterministic(
        art, rubric, core=core,
        mission_ref=None if mission is None else mission.get("mission_ref"),
    )
    judgement = None
    verification = None
    if model is not None:
        if mission is None:
            raise ResearchQualityValidationError("the judge layer needs the mission it is billed to")
        judgement = judge(
            art, rubric, deterministic, model=model, mission=mission,
            request_id=request_id or f"{rubric.rubric_ref}:{art['ref']}:{art['hash'][:16]}",
        )
        if verifier_model is not None:
            verification = verify(
                art, rubric, judgement, model=verifier_model, mission=mission,
                request_id=f"verify:{request_id or art['ref']}",
            )
    return {
        "rubric_ref": rubric.rubric_ref,
        "rubric_hash": rubric.content_hash,
        "target_ref": art["ref"],
        "target_hash": art["hash"],
        "artefact_kind": art["artefact_kind"],
        "deterministic": deterministic,
        "judge": judgement,
        "verifier": verification,
    }


__all__ = [
    "ARTEFACT_KINDS",
    "WEEKLY_BRIEF_CAPABILITY_SOURCES",
    "CHECKS",
    "INITIAL_SCREEN_SECTIONS",
    "JUDGE_MODEL_CONFIG_NAME",
    "VERIFIER_PURPOSE",
    "JUDGE_PURPOSE",
    "MAX_ARTEFACT_CHARS",
    "MAX_COST_USD",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "QualityScoreAuthority",
    "RESTATEMENT_SIMILARITY",
    "ResearchQualityConflict",
    "ResearchQualityError",
    "ResearchQualityValidationError",
    "SCORER_VERSION",
    "ScoringIdentity",
    "TIMEOUT_SECONDS",
    "VERIFIER_FINDING_CODES",
    "VERIFIER_VERDICTS",
    "artefact",
    "artefact_from_ask_answer",
    "artefact_from_deliverable",
    "artefact_from_dossier",
    "artefact_from_weekly_brief",
    "build_judge_prompt",
    "build_verifier_prompt",
    "judge",
    "residual_citation_artefacts",
    "run_deterministic",
    "score_artefact",
    "summarise_scores",
    "validate_judge_output",
    "validate_verifier_output",
    "verify",
    "weekly_brief_capabilities",
    "withheld_criteria",
]
