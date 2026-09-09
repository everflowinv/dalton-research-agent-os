"""Q1: the analyst journal -- what the PM said about what Dalton wrote.

The Playbook's own description of a Basic Level 1 analyst, the level Dalton is
at, ends with a duty it has never been able to discharge: 建立 analyst journal
（内化反馈、避免重复错误）.  Feedback has had nowhere to go.  The owner reads an
Initial Screen, forms a view of it, and the view evaporates; the next draft is
written by a system that has never been told anything.

An entry is small on purpose.  A closed five-word vocabulary (``read``,
``useful``, ``needs_more_evidence``, ``disagree``, ``revise``), optional prose,
and an optional score override for when a person disagrees with a rubric score.
Five words are enough to steer drafting and few enough that the PM can answer
in one click, which is the difference between a feedback loop that exists and
one that is documented.

Two bindings make an entry mean something later:

- **the target's content hash.**  "This is good" said about a document that has
  since been rewritten is a statement about the old document.  A journal that
  cannot tell says the new one is good too.
- **the company.**  The reader that drafting will use is per-company: what the
  PM has been asking for about Accenture, across every artefact, in the order
  it was said.

Nothing here decides anything.  ``journal_context`` renders the entries in the
shape a drafting prompt can carry; whether a drafter reads it is that drafter's
choice, and today none does -- the writer op and the cockpit buttons are the
integration this slice describes but does not wire.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("analyst_journal_schema.sql")

# The closed vocabulary.  Ordered from "I looked at it" to "do it again",
# because the order is what makes a run of entries readable as a trajectory.
VERDICTS: tuple[str, ...] = ("read", "useful", "needs_more_evidence", "disagree", "revise")
VERDICT_LABELS: Mapping[str, str] = {
    "read": "读过",
    "useful": "有用",
    "needs_more_evidence": "证据不够",
    "disagree": "不同意",
    "revise": "要重写",
}
TARGET_KINDS: tuple[str, ...] = (
    "initial_screen", "ask_answer", "company_dossier", "quality_score", "claim", "weekly_brief",
)
MAX_NOTE_CHARS = 4000
# What a drafting prompt may carry of the journal.  A journal that grows without
# bound would eventually be the prompt.
MAX_CONTEXT_ENTRIES = 20
MAX_CONTEXT_NOTE_CHARS = 400


class AnalystJournalError(RuntimeError):
    """Base error for the analyst journal."""


class AnalystJournalValidationError(AnalystJournalError):
    """A closed field or argument is invalid."""


class AnalystJournalConflict(AnalystJournalError):
    """An append-only entry was reused with different content."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise AnalystJournalValidationError(
            f"{name} must be text of {minimum}..{maximum} characters"
        )
    return value.strip()


def validate_score_override(value: Any) -> dict[str, Any] | None:
    """A person's own scores, in the rubric's own terms.

    An override is not a correction of the judge -- nothing rewrites the score
    record -- it is a second, human reading recorded beside it.  Which is why
    it carries the rubric it is speaking about: a number with no rubric is a
    number with no meaning.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) - {"rubric_ref", "scores", "overall"} or "rubric_ref" not in value:
        raise AnalystJournalValidationError(
            "score_override must carry rubric_ref and at least one of scores or overall"
        )
    override: dict[str, Any] = {"rubric_ref": _text(value["rubric_ref"], "rubric_ref", maximum=256)}
    scores = value.get("scores")
    if scores is not None:
        if not isinstance(scores, Mapping) or not scores:
            raise AnalystJournalValidationError("score_override.scores must be a non-empty object")
        checked: dict[str, int] = {}
        for criterion_id, score in scores.items():
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 4:
                raise AnalystJournalValidationError(
                    f"score_override.scores.{criterion_id} must be a whole number 0..4"
                )
            checked[str(criterion_id)] = score
        override["scores"] = dict(sorted(checked.items()))
    overall = value.get("overall")
    if overall is not None:
        if isinstance(overall, bool) or not isinstance(overall, int) or not 0 <= overall <= 4:
            raise AnalystJournalValidationError("score_override.overall must be a whole number 0..4")
        override["overall"] = overall
    if "scores" not in override and "overall" not in override:
        raise AnalystJournalValidationError(
            "score_override must carry at least one of scores or overall"
        )
    return override


class AnalystJournalAuthority:
    """Append-only PM feedback on any artefact, bound to what was read."""

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorized = False
        self.connection.create_function(
            "dalton_analyst_journal_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("AnalystJournalAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- write --------------------------------------------------------------

    def add(
        self,
        *,
        target_ref: str,
        target_hash: str,
        target_kind: str,
        verdict: str,
        actor_ref: str,
        company_ref: str | None = None,
        note: str | None = None,
        score_override: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        target_ref = _text(target_ref, "target_ref", maximum=512)
        target_hash = _text(target_hash, "target_hash", maximum=128)
        if target_kind not in TARGET_KINDS:
            raise AnalystJournalValidationError(f"target_kind must be one of {list(TARGET_KINDS)}")
        if verdict not in VERDICTS:
            raise AnalystJournalValidationError(f"verdict must be one of {list(VERDICTS)}")
        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        # A journal of what a person thought is a journal of what a *person*
        # thought.  Automation grading itself is the quality score, and it has
        # its own record; letting it write here would make the one place that
        # carries human judgement stop being one.
        if not actor_ref.startswith("human:"):
            raise AnalystJournalValidationError("analyst journal entries need a human: principal")
        if company_ref is not None:
            company_ref = _text(company_ref, "company_ref", maximum=256)
        if note is not None:
            note = _text(note, "note", maximum=MAX_NOTE_CHARS)
        override = validate_score_override(score_override)
        record = {
            "schema_version": SCHEMA_VERSION,
            "target_ref": target_ref,
            "target_hash": target_hash,
            "target_kind": target_kind,
            "company_ref": company_ref,
            "verdict": verdict,
            "note": note,
            "score_override": override,
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        with self._transaction() as cur:
            if idempotency_key is not None:
                key = _text(idempotency_key, "idempotency_key", maximum=512)
                seen = cur.execute(
                    "SELECT record_json FROM analyst_journal_entries WHERE target_ref=? "
                    "AND idempotency_key=?", (target_ref, key),
                ).fetchone()
                if seen is not None:
                    return {**json.loads(seen["record_json"]), "status": "duplicate"}
                record["idempotency_key"] = key
            row = cur.execute(
                "SELECT MAX(entry_number) AS highest FROM analyst_journal_entries WHERE target_ref=?",
                (target_ref,),
            ).fetchone()
            number = 1 if row is None or row["highest"] is None else int(row["highest"]) + 1
            record["entry_number"] = number
            record["id"] = (
                "analyst-journal-entry:"
                + content_hash({"target_ref": target_ref, "entry_number": number})[:32]
            )
            record["content_hash"] = content_hash(
                {k: v for k, v in record.items() if k != "content_hash"}
            )
            cur.execute(
                "INSERT INTO analyst_journal_entries(entry_id,entry_number,target_ref,target_hash,"
                "target_kind,company_ref,verdict,note,score_override_json,idempotency_key,"
                "record_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], number, target_ref, target_hash, target_kind, company_ref, verdict,
                 note, None if override is None else json.dumps(override, ensure_ascii=False, sort_keys=True),
                 record.get("idempotency_key"),
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
        written = self.entry(record["id"])
        if written is None or written["content_hash"] != record["content_hash"]:
            raise AnalystJournalConflict("the journal entry did not read back")
        return {**written, "status": "fresh"}

    # -- reads --------------------------------------------------------------

    def entry(self, entry_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json, content_hash FROM analyst_journal_entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise AnalystJournalConflict("analyst journal drifted")
        return record

    def for_target(self, target_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM analyst_journal_entries WHERE target_ref=? ORDER BY entry_number",
            (_text(target_ref, "target_ref", maximum=512),),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def for_company(self, company_ref: str, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM analyst_journal_entries WHERE company_ref=? "
            "ORDER BY created_at, entry_number LIMIT ?",
            (_text(company_ref, "company_ref", maximum=256), max(1, min(int(limit), 1000))),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def outstanding(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        """Entries asking for something: needs_more_evidence, disagree, revise.

        The ones a drafter should read first, because they are the ones that
        say the last attempt was not enough.
        """

        entries = (
            self.for_company(company_ref) if company_ref is not None
            else [json.loads(row["record_json"]) for row in self.connection.execute(
                "SELECT record_json FROM analyst_journal_entries ORDER BY created_at"
            ).fetchall()]
        )
        wanted = {"needs_more_evidence", "disagree", "revise"}
        return [entry for entry in entries if entry["verdict"] in wanted]


def journal_context(
    entries: Sequence[Mapping[str, Any]], *, limit: int = MAX_CONTEXT_ENTRIES
) -> dict[str, Any]:
    """The journal in the shape a drafting prompt can carry.

    Most recent last, because that is the order the writer will read it in, and
    the last thing the PM said should be the last thing in the prompt.
    """

    ordered = sorted(entries, key=lambda item: (str(item.get("created_at") or ""), item.get("entry_number", 0)))
    kept = ordered[-limit:]
    lines = []
    for entry in kept:
        note = (entry.get("note") or "")[:MAX_CONTEXT_NOTE_CHARS]
        label = VERDICT_LABELS.get(str(entry.get("verdict")), str(entry.get("verdict")))
        when = str(entry.get("created_at") or "")[:10]
        target = str(entry.get("target_ref") or "")
        line = f"{when} PM 对 {target} 的反馈：{label}"
        if note:
            line += f"——{note}"
        lines.append(line)
    return {
        "entries": len(kept),
        "of": len(ordered),
        "verdicts": sorted({str(entry.get("verdict")) for entry in kept}),
        "lines": lines,
        "text": "\n".join(lines),
    }


def render_journal_for_prompt(entries: Sequence[Mapping[str, Any]]) -> str:
    """One block a drafting prompt can paste in, or an empty string."""

    context = journal_context(entries)
    if not context["lines"]:
        return ""
    return (
        "PM 对这家公司过往交付物的反馈（analyst journal，最新在最后）：\n"
        + context["text"]
        + "\n把这些反馈当作对下一稿的要求：被要求补证据的地方不要再用同样的材料交一遍。"
    )


__all__ = [
    "AnalystJournalAuthority",
    "AnalystJournalConflict",
    "AnalystJournalError",
    "AnalystJournalValidationError",
    "MAX_CONTEXT_ENTRIES",
    "SCHEMA_VERSION",
    "TARGET_KINDS",
    "VERDICTS",
    "VERDICT_LABELS",
    "journal_context",
    "render_journal_for_prompt",
    "validate_score_override",
]
