"""W3: an earlier Initial Screen becomes v0, and Dalton still writes v1.

The owner's sentence, which is the whole design: *a new analyst reads the old
screen first — it saves a week — and then writes their own anyway, because the
material may be stale and the market's questions have moved.*

So an imported prior screen is not the current view. It is **version zero** of
the company's deliverable chain: the place the fund's thinking actually
started, kept so that the chain reads as a history rather than beginning
mid-sentence. Four rules follow from that, and each of them is a refusal
somewhere in this module or in ``mission_deliverable``:

1. **v0 asserts nothing.** Its ``change_reason`` is ``imported_prior``, which
   is legal on no other version. Its figures are recorded as gaps rather than
   refused, because the figure rule exists to stop *this system* writing a
   number it cannot cite, and a v0 is a record of what a document said.
2. **v0 never passes the gate.** The stored self-assessment marks every item
   ``imported``, not ``passed``. Answering the fund's own exit gate about a
   document the fund wrote in 2024 would be a fabrication whichever way it
   came out.
3. **v0 does not count as having a screen.** Selection reads a chain whose
   head is v0 as "no screen yet", so Dalton drafts v1 — and v1 gets
   ``prior_version_ref`` pointing at v0 for free, because the authority takes
   it from the pointer.
4. **The drafter is shown v0 and told to judge it.** The ``prior_reference``
   block carries the prior sections, their refs and their age in months, plus
   the standing instruction: for every prior question and debate, say whether
   it still holds, has changed, or has been answered — with refs — and never
   copy a conclusion.

The fifth piece is ``delta_vs_prior``: what actually happened between the two
versions. Deterministic where it can be — filings landed, price moved, debates
shifted — and honestly absent where the material is not on this Core, because
"no market data" and "the price did not move" are different sentences.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Mapping, Sequence

from .initial_screen import GATE_ITEM_STATUSES, GATE_QUESTION_CHECKS, SCHEMA_VERSION
from .mission_deliverable import IMPORT_CHANGE_REASON, MAX_SECTIONS
from .prior_research_core import age_in_months

KIND = "initial_screen"
TEMPLATE_REF = "template:initial-screen:imported-prior:0.1"

#: Markdown headings, and the two Chinese section markers a hand-written screen
#: tends to use. Deliberately narrow: a screen that does not split is imported
#: as one section, which is honest, rather than being cut on a guess.
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,4})\s*(.+?)\s*#*\s*$")
_NUMBERED_RE = re.compile(r"^\s{0,3}([一二三四五六七八九十]+|\d{1,2})[、.)]\s*(\S.{0,80})$")

MAX_SECTION_BODY_CHARS = 6_000
DEFAULT_SECTION_TITLE = "上一版正文"


def split_sections(text: str) -> list[dict[str, Any]]:
    """A prior document's own sections, or one section holding all of it.

    No model is asked to summarise or restructure. The document keeps the
    shape its author gave it, and a document with no headings arrives as one
    long section -- which reads badly and is true, where a machine-invented
    outline would read well and be a claim about somebody else's writing.
    """

    if not isinstance(text, str):
        raise ValueError("prior document text must be a string")
    sections: list[dict[str, Any]] = []
    title = DEFAULT_SECTION_TITLE
    body: list[str] = []

    def flush() -> None:
        joined = "\n".join(body).strip()
        if joined or sections:
            sections.append({"title": title, "body": joined[:MAX_SECTION_BODY_CHARS]})

    for line in text.splitlines():
        heading = _HEADING_RE.match(line) or _NUMBERED_RE.match(line)
        if heading is not None and heading.group(2).strip():
            flush()
            title = heading.group(2).strip()[:200]
            body = []
            continue
        body.append(line)
    flush()
    kept = [item for item in sections if item["body"]]
    if not kept:
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("a prior screen with no text is not a document")
        kept = [{"title": DEFAULT_SECTION_TITLE, "body": cleaned[:MAX_SECTION_BODY_CHARS]}]
    return kept[:MAX_SECTIONS]


def imported_gate(
    *,
    playbook: Mapping[str, Any],
    document_ref: str,
    as_of: str,
) -> dict[str, Any]:
    """The exit-gate self-assessment stored on an imported v0.

    Every item ``imported``. Not ``passed``, because nothing was checked, and
    not ``failed``, because nothing failed: the document was written outside
    this system and its author was answering different questions.
    """

    stage = next(
        (item for item in playbook.get("stages", ())
         if item.get("stage_ref") == "initial_screen"),
        None,
    )
    questions = list((stage or {}).get("exit_gate", {}).get("questions") or [])
    answers = [
        {
            "check": check,
            "status": "imported",
            "question": questions[index] if index < len(questions) else check,
            "basis": (
                f"这一版是 {as_of} 的既有文档（{document_ref}），不是本系统写的；"
                "出口门四问未在此文档上核对"
            ),
        }
        for index, check in enumerate(GATE_QUESTION_CHECKS)
    ]
    assert all(item["status"] in GATE_ITEM_STATUSES for item in answers)
    return {
        "schema_version": SCHEMA_VERSION,
        # A v0 is never a passed gate. The word is a state of a version
        # (ADR-0008) and this version's state is "imported".
        "passed": False,
        "imported": True,
        "answers": answers,
        "delta_vs_prior": None,
        "rationale": "既有资料导入，未经本系统的出口门自评",
    }


def import_prior_screen(
    authority: Any,
    *,
    mission: Mapping[str, Any],
    playbook: Mapping[str, Any],
    company_ref: str,
    document: Mapping[str, Any],
    text: str,
    actor_ref: str,
    extra_refs: Sequence[str] = (),
) -> dict[str, Any]:
    """Store one prior Initial Screen as v0 of this company's chain.

    ``document`` is the prior-research wire header: it carries the document
    id, the manifest's ``as_of``, the author and the source note, and those go
    into the summary so that a reader of v0 is told, in v0, that they are
    reading somebody's file from a date.
    """

    document_ref = str(document["document_id"])
    as_of = str(document["as_of"])
    author = str(document.get("author") or "").strip()
    note = str(document.get("source_note") or "").strip()
    sections = split_sections(text)
    byline = f"，作者 {author}" if author else ""
    tail = f"；来源说明：{note}" if note else ""
    summary = (
        f"上一版 Initial Screen（内部既有资料，{as_of}{byline}），导入为版本 0。"
        f"结论未经本系统核对，数字未绑定 Claim{tail}"
    )[:2000]
    refs = [document_ref, *[str(ref) for ref in extra_refs if str(ref).strip()]]
    return authority.publish(
        kind=KIND,
        subject_ref=company_ref,
        mission=mission,
        playbook=playbook,
        template_ref=TEMPLATE_REF,
        sections=sections,
        summary=summary,
        gaps=[
            "这一版的每一条判断都要在新版本里逐条重判：仍然成立 / 已经变了 / 已经有答案",
        ],
        actor_ref=actor_ref,
        revision={"change_reason": IMPORT_CHANGE_REASON, "evidence_refs": refs},
        as_version_zero=True,
        gate=imported_gate(
            playbook=playbook, document_ref=document_ref, as_of=as_of
        ),
        idempotency_key=f"prior-screen-import:{document_ref}",
    )


# -- what the drafter is shown ------------------------------------------


def prior_reference(
    version: Mapping[str, Any] | None, *, now: date | None = None
) -> dict[str, Any] | None:
    """The 「上一版（内部，YYYY-MM）」 block for the drafting context.

    ``None`` when there is no previous version, which is what most companies
    look like. The age is in months because that is the unit the judgement
    actually turns on: four months and fourteen are different questions, 181
    days and 184 are the same one.
    """

    if not version:
        return None
    as_of = _version_as_of(version)
    months = None
    if as_of:
        try:
            months = age_in_months(as_of, now=now or date.today())
        except ValueError:
            months = None
    return {
        "version_ref": version.get("id"),
        "version": version.get("version"),
        "as_of": as_of,
        "age_months": months,
        "imported": version.get("version") == 0,
        "summary": version.get("summary") or "",
        "sections": [
            {
                "title": section.get("title") or "",
                "body": section.get("body") or "",
                "gaps": list(section.get("gaps") or ()),
            }
            for section in (version.get("sections") or ())
        ],
        "refs": list(((version.get("revision") or {}).get("evidence_refs")) or ()),
    }


def _version_as_of(version: Mapping[str, Any]) -> str:
    """The date a prior version speaks for.

    For an imported v0 that is the manifest's ``as_of``, which the import put
    into the ``summary`` and the evidence refs; for a version this system
    wrote it is ``created_at``. Falling back to ``created_at`` for an import
    would date a 2024 memo to the day it was read, which is the exact mistake
    the feed refuses to make at the other end -- so the summary is parsed
    rather than assumed, and an unparseable one yields no date at all.
    """

    created = str(version.get("created_at") or "")
    if version.get("version") != 0:
        return created[:10]
    found = re.search(r"(\d{4}-\d{2}-\d{2})", str(version.get("summary") or ""))
    return found.group(1) if found else ""


# -- what changed since ---------------------------------------------------


def build_delta_vs_prior(
    *,
    prior: Mapping[str, Any] | None,
    filings: Sequence[Mapping[str, Any]] = (),
    price_move: Mapping[str, Any] | None = None,
    shifted_debates: Sequence[Mapping[str, Any]] = (),
    now: date | None = None,
) -> dict[str, Any] | None:
    """What happened between the previous version and this one.

    Every field is either a counted fact or an explicit absence. "No market
    data on this Core" and "the price did not move" are different sentences
    and this returns different things for them, because a re-issue that says
    "price flat" when it never looked is worse than one that says it did not
    look.
    """

    if not prior:
        return None
    reference = prior.get("as_of") or ""
    months = prior.get("age_months")
    landed = [
        {
            "ref": str(item.get("ref") or item.get("accession") or ""),
            "form": str(item.get("form") or ""),
            "filed_at": str(item.get("filed_at") or item.get("period_end") or ""),
        }
        for item in filings
        if not reference or str(item.get("filed_at") or item.get("period_end") or "") > reference
    ]
    shifted = [
        {
            "debate_ref": str(item.get("debate_ref") or ""),
            "question": str(item.get("question") or "")[:300],
            "status": str(item.get("status") or ""),
            "last_shift_reason": str(item.get("last_shift_reason") or "")[:300],
        }
        for item in shifted_debates
    ]
    return {
        "prior_version_ref": prior.get("version_ref"),
        "prior_as_of": reference,
        "age_months": months,
        "new_filings": landed,
        "new_filing_count": len(landed),
        "price_move": (
            dict(price_move) if price_move is not None
            else {"available": False,
                  "reason": "this Core holds no market price series for the window"}
        ),
        "debates_shifted": shifted,
        "debates_shifted_count": len(shifted),
        "assessed_on": (now or date.today()).isoformat(),
    }


__all__ = [
    "DEFAULT_SECTION_TITLE",
    "KIND",
    "MAX_SECTION_BODY_CHARS",
    "TEMPLATE_REF",
    "build_delta_vs_prior",
    "import_prior_screen",
    "imported_gate",
    "prior_reference",
    "split_sections",
]
