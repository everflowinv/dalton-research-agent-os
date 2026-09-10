"""P10c: the mission's own documents, with every number traceable (vision v1.1).

Phase 9 froze what an Initial Screen contains (the Playbook's
``deliverable_templates.initial_screen``) and what it must satisfy to pass its
gate.  P9d filled the Ledger with Claims.  This module is where the two meet:
a versioned, append-only document per company, drafted from Claims the Ledger
already holds, and refused if it says a number it cannot trace.

Three rules the authority enforces rather than trusts:

- **Every number traces to a quantitative Claim.**  A section body may only
  carry a figure that appears in that section's ``numbers`` list bound to a
  quantitative claim version.  A period label (a year, a quarter, a fiscal
  year) is not a figure, the same distinction the drafting path already makes.
  An unsourced figure fails the publish; the drafter is expected to write
  "缺来源" instead of guessing.
- **Every cited Claim exists and is not retired.**  A section's ``claim_refs``
  must name live claim versions (P10b retirements are excluded), so a document
  can never rest on a Claim the system has already disowned.
- **The document binds the authorities it was written under.**  The mission
  version and the playbook version, both by hash: if either moves, the next
  version records the move instead of the text silently drifting.

Automation may publish only what the mission grants (``deliverable`` in
``autonomy.may_write``); a person may always publish.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("mission_deliverable_schema.sql")
WRITE_SCOPE = "deliverable"
DELIVERABLE_KINDS: tuple[str, ...] = (
    "industry_framework", "initial_screen", "industry_model", "company_model",
    "forecast_lines", "investment_memo", "weekly_brief",
    # P14a: the short note a tracked event produces -- "ACN fell 4.2% today:
    # sector-wide, no company news; the nearest driver is X".  One chain per
    # company, gaining a version per note, so the company's running commentary
    # is replayable by version like every other output (ADR-0008).  It is a
    # deliverable rather than a new object precisely so that it inherits the
    # rule that a figure with no live Claim behind it is refused.
    "event_note",
)
#: The one ``change_reason`` a version zero may carry, and the only version it
#: may be carried on. Named here so the two rules read as one thing.
IMPORT_CHANGE_REASON = "imported_prior"
MAX_SECTIONS = 24
MAX_BODY_CHARS = 6000
GAP_MARKER = "缺来源"

# A year, quarter, half or fiscal-year label is a period, not a figure.  Same
# rule the extraction contract uses, so the two paths agree on what a number is.
_PERIOD_TOKEN_RE = re.compile(
    # ISO dates and the ".." ranges the Ledger prints for a reporting period,
    # which live were being read apart into "01" and "31".
    r"(?:19|20)\d{2}-\d{2}-\d{2}(?:\s*\.\.\s*(?:19|20)?\d{2}-\d{2}-\d{2})?"
    r"|(?:FY\s?)?(?:19|20)\d{2}(?:\s?[-–/]\s?(?:19|20)?\d{2})?(?:\s?(?:年|财年))?"
    r"|FY\s?\d{2}(?![0-9])"
    r"|Q[1-4]\s?(?:FY\s?)?(?:19|20)?\d{0,4}"
    r"|(?:19|20)\d{2}\s?Q[1-4]"
    r"|[1-4]Q(?:19|20)?\d{2}"
    r"|H[12]\s?(?:19|20)?\d{0,4}"
    r"|第?[一二三四1-4]季度",
    re.IGNORECASE,
)
# The Playbook's rule is about *timely numbers*: a measurement that has to come
# from a filing or a tool result.  A bare small integer with no unit, percent,
# currency or separator is a threshold, a count or an ordinal ("book-to-bill
# 跌破 1", "两条线"), not a measurement, and requiring a Claim for it would
# empty the document without making it truer.
_BARE_SMALL_INTEGER = 12
# The longest rendering of one cited figure. A Claim's normalized_statement has
# no ceiling in its own contract, so this is generous on purpose: it exists to
# stop a single statement becoming a document, not to police wording. The
# document's real bound is the 60-entry cap on a section's numbers.
MAX_NUMBER_TEXT = 1000
_VALUE_TOKEN_RE = re.compile(r"[$€£¥]\s?\d[\d,.]*|\d[\d,.]*\s?%|\d[\d,.]*")


class MissionDeliverableError(RuntimeError):
    """Base error for the deliverable authority."""


class MissionDeliverableValidationError(MissionDeliverableError):
    """A closed field or argument is invalid."""


class MissionDeliverableConflict(MissionDeliverableError):
    """An append-only record was reused with different semantics, or a gate refused."""


class MissionDeliverableNotFound(MissionDeliverableError):
    """The named record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 2000, minimum: int = 1) -> str:
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise MissionDeliverableValidationError(f"{name} must be text of {minimum}..{maximum} characters")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MissionDeliverableValidationError(f"{name} must be a SHA-256 hex digest")
    return value


# A drafted body may still carry a "C7" / "N1" citation tag; its digits are a
# reference, not a figure.  Live, they were the first thing the check flagged.
_CITATION_TAG_RE = re.compile(r"(?<![A-Za-z0-9])[CN]\d{1,3}(?![A-Za-z0-9])")


def value_tokens(text: str) -> list[str]:
    """Every figure the text asserts, with period labels and citation tags removed."""

    without_tags = _CITATION_TAG_RE.sub(" ", text or "")
    without_periods = _PERIOD_TOKEN_RE.sub(" ", without_tags)
    tokens = []
    for match in _VALUE_TOKEN_RE.finditer(without_periods):
        token = match.group(0).strip()
        plain = token.rstrip(".")
        if plain.isdigit() and int(plain) <= _BARE_SMALL_INTEGER:
            continue
        tokens.append(token)
    return tokens


def _normalise_number(token: str) -> str:
    return re.sub(r"[\s,]", "", token).rstrip(".")


def unsourced_numbers(body: str, numbers: Sequence[Mapping[str, Any]]) -> list[str]:
    """Figures in the body that no supplied, Claim-bound number accounts for."""

    sourced = set()
    for item in numbers:
        for token in value_tokens(str(item.get("text", ""))):
            sourced.add(_normalise_number(token))
    return [
        token for token in value_tokens(body)
        if _normalise_number(token) not in sourced
    ]


# ADR-0008's closed vocabulary.  Imported rather than restated so a sixth
# reason has one place to be added, and so the cockpit's label map, which
# already renders these five, cannot drift away from the authority that
# accepts them.
REVISION_FIELDS: frozenset[str] = frozenset({
    "change_reason", "evidence_refs", "reopen_ref",
})


def validate_revision(value: Any) -> dict[str, Any] | None:
    """Why a version exists, or ``None`` when nobody claimed a reason.

    ADR-0008: a version carries a ``change_reason`` from a closed vocabulary
    and the exact evidence refs that occasioned it, and "a version that cannot
    name what it learned is a rewrite".  So a reason with no refs is refused
    here rather than stored and disbelieved later.
    """

    if value is None:
        return None
    from .model_forecast_driver import CHANGE_REASONS

    if not isinstance(value, Mapping):
        raise MissionDeliverableValidationError("revision must be an object")
    unknown = sorted(set(value) - REVISION_FIELDS)
    if unknown:
        raise MissionDeliverableValidationError(
            f"revision has unknown fields: {', '.join(unknown)}"
        )
    reason = value.get("change_reason")
    if reason not in CHANGE_REASONS:
        raise MissionDeliverableValidationError(
            f"change_reason must be one of {list(CHANGE_REASONS)}"
        )
    refs = value.get("evidence_refs") or ()
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise MissionDeliverableValidationError("revision evidence_refs must be a list")
    cleaned = [_text(ref, "revision.evidence_refs[]", maximum=512) for ref in refs]
    if not cleaned:
        raise MissionDeliverableValidationError(
            "a revision must name the evidence that occasioned it (ADR-0008)"
        )
    reopen_ref = value.get("reopen_ref")
    if reopen_ref is not None:
        reopen_ref = _text(reopen_ref, "revision.reopen_ref", maximum=512)
    return {
        "change_reason": reason,
        "evidence_refs": list(dict.fromkeys(cleaned))[:40],
        "reopen_ref": reopen_ref,
    }


#: The gap a figure in an imported prior document is recorded as. It is not a
#: refusal, because an import asserts nothing: the figure rule exists to stop
#: *this system* writing a number it cannot cite, and a v0 is a record of what
#: a document said, bound to that document by ref and hash.
IMPORTED_FIGURE_GAP = "上一版里的数字，未在本系统重新核对"


def validate_section(
    section: Mapping[str, Any],
    *,
    live_claim_refs: set[str] | None = None,
    imported: bool = False,
) -> dict[str, Any]:
    """One closed section: a title, a body or a gap, and traceable numbers.

    ``imported`` relaxes exactly one rule and no others: a figure in the body
    with no Claim behind it becomes a recorded gap instead of a refusal. An
    imported v0 is a prior document, not an assertion this system is making,
    and the alternative -- stripping the numbers out of an old screen so it
    would pass -- would destroy the thing being imported. Every other check
    still applies, and a v0 still may not cite a Claim that does not exist.
    """

    if not isinstance(section, Mapping):
        raise MissionDeliverableValidationError("section must be an object")
    allowed = {"title", "body", "claim_refs", "numbers", "gaps"}
    if set(section) - allowed or "title" not in section:
        raise MissionDeliverableValidationError("section has an invalid closed shape")
    title = _text(section["title"], "section.title", maximum=200)
    body = section.get("body") or ""
    if not isinstance(body, str) or len(body) > MAX_BODY_CHARS:
        raise MissionDeliverableValidationError("section.body must be text under 6000 characters")
    numbers = section.get("numbers") or []
    if not isinstance(numbers, list) or len(numbers) > 60:
        raise MissionDeliverableValidationError("section.numbers must be a list of at most 60 entries")
    checked: list[dict[str, Any]] = []
    for item in numbers:
        if not isinstance(item, Mapping) or set(item) - {"text", "claim_version_ref", "period"} or not {
            "text", "claim_version_ref"
        } <= set(item):
            raise MissionDeliverableValidationError("number entries need text and claim_version_ref")
        ref = _text(item["claim_version_ref"], "number.claim_version_ref", maximum=512)
        if live_claim_refs is not None and ref not in live_claim_refs:
            raise MissionDeliverableConflict(
                "a figure cites a Claim that is retired or does not exist: " + ref
            )
        checked.append({
            # P13ac: a number's text is the Claim's own normalized_statement,
            # and the Claim contract puts no ceiling on that. 200 was narrower
            # than what the system legitimately produces: the SEC lane writes
            # "EPAM SYSTEMS, INC. reported Revenue from Contract with Customer,
            # Excluding Assessed Tax of ..." at 205 characters, so every EPAM
            # figure failed here and the Initial Screen could not be published
            # at all -- permanently, because Claims are append-only and cannot
            # be shortened after the fact.
            #
            # Eliding instead of widening would be worse: unsourced_numbers
            # reads the figures back out of this text, and these statements put
            # the figure last, so a truncation would drop the number and then
            # report the body that cites it as unsourced.
            #
            # The document stays bounded by the 60-entry cap above; this bound
            # is only here so one statement cannot be a document.
            "text": _text(item["text"], "number.text", maximum=MAX_NUMBER_TEXT),
            "claim_version_ref": ref,
            "period": None if item.get("period") is None else _text(item["period"], "number.period", maximum=120),
        })
    claim_refs = section.get("claim_refs") or []
    if not isinstance(claim_refs, list) or len(claim_refs) > 200 or any(
        not isinstance(ref, str) or not ref for ref in claim_refs
    ):
        raise MissionDeliverableValidationError("section.claim_refs must be a list of refs")
    if live_claim_refs is not None:
        missing = [ref for ref in claim_refs if ref not in live_claim_refs]
        if missing:
            raise MissionDeliverableConflict(
                "a section cites Claims that are retired or do not exist: " + ", ".join(missing[:3])
            )
    gaps = section.get("gaps") or []
    if not isinstance(gaps, list) or len(gaps) > 20 or any(
        not isinstance(gap, str) or not gap.strip() for gap in gaps
    ):
        raise MissionDeliverableValidationError("section.gaps must be a list of short strings")
    stray = unsourced_numbers(body, checked)
    if stray and not imported:
        raise MissionDeliverableConflict(
            f"section「{title}」carries figures with no Claim behind them: {stray[:5]}；"
            f"写 {GAP_MARKER} 而不是猜一个数字"
        )
    recorded = [gap.strip()[:300] for gap in gaps]
    if stray and imported:
        recorded.append(f"{IMPORTED_FIGURE_GAP}：{'、'.join(stray[:5])}")
    return {
        "title": title, "body": body.strip(), "claim_refs": list(dict.fromkeys(claim_refs)),
        "numbers": checked, "gaps": recorded[:20],
    }


class MissionDeliverableAuthority:
    """Append-only, hash-bound documents, refused when a number has no source."""

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorized = False
        self.connection.create_function(
            "dalton_mission_deliverable_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._widen_kind_check()
        self._admit_version_zero()

    def _admit_version_zero(self) -> None:
        """W3: admit v0 on a Core built when 1 was the floor.

        Same shape and the same reason as ``_widen_kind_check`` above: the
        constraint lives in the table, ``CREATE TABLE IF NOT EXISTS`` does not
        revisit a table that exists, and an unmigrated Core would refuse the
        import with an ``IntegrityError`` rather than a sentence. Sentinelled
        on the constraint text itself, so this runs at most once.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='mission_deliverable_versions'"
        ).fetchone()
        if row is None or "version_number >= 0" in (row["sql"] or ""):
            return
        if self.connection.in_transaction:
            raise MissionDeliverableConflict(
                "the deliverable version-zero migration requires no open transaction"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS mission_deliverables_authorized_insert;
                DROP TRIGGER IF EXISTS mission_deliverables_no_update;
                DROP TRIGGER IF EXISTS mission_deliverables_no_delete;
                CREATE TABLE mission_deliverable_versions_v3 (
                    version_id TEXT PRIMARY KEY,
                    deliverable_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 0),
                    prior_version_ref TEXT REFERENCES mission_deliverable_versions_v3(version_id),
                    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
                    mission_version_hash TEXT NOT NULL,
                    playbook_version_ref TEXT NOT NULL,
                    playbook_version_hash TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN (
                        'industry_framework','initial_screen','industry_model','company_model',
                        'forecast_lines','investment_memo','weekly_brief','event_note'
                    )),
                    subject_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deliverable_ref, version_number)
                );
                INSERT INTO mission_deliverable_versions_v3 SELECT * FROM mission_deliverable_versions;
                DROP TABLE mission_deliverable_versions;
                ALTER TABLE mission_deliverable_versions_v3 RENAME TO mission_deliverable_versions;
                CREATE INDEX IF NOT EXISTS idx_mission_deliverables_by_subject
                ON mission_deliverable_versions(mission_version_ref, subject_ref, kind, created_at);
                CREATE TRIGGER mission_deliverables_authorized_insert
                BEFORE INSERT ON mission_deliverable_versions
                WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'mission deliverable insert requires MissionDeliverableAuthority'); END;
                CREATE TRIGGER mission_deliverables_no_update
                BEFORE UPDATE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                CREATE TRIGGER mission_deliverables_no_delete
                BEFORE DELETE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                COMMIT;
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MissionDeliverableConflict(
                "the deliverable version-zero migration broke foreign keys"
            )

    def _widen_kind_check(self) -> None:
        """P14a: admit ``event_note`` on a Core built before that kind existed.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that is already
        there, so a Core created under the seven-kind CHECK keeps refusing the
        eighth for ever -- with an ``IntegrityError`` from the constraint
        rather than the readable refusal the vocabulary check above gives.
        Live holds a handful of deliverables, so the rebuild is small; it
        follows ``DaltonStore._migrate_thesis_authority_columns`` exactly,
        including the foreign-key check afterwards, because a rebuild that
        silently orphaned the pointer would be worse than the constraint.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='mission_deliverable_versions'"
        ).fetchone()
        if row is None or "'event_note'" in (row["sql"] or ""):
            return
        if self.connection.in_transaction:
            raise MissionDeliverableConflict(
                "the deliverable kind migration requires no open transaction"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS mission_deliverables_authorized_insert;
                DROP TRIGGER IF EXISTS mission_deliverables_no_update;
                DROP TRIGGER IF EXISTS mission_deliverables_no_delete;
                CREATE TABLE mission_deliverable_versions_v2 (
                    version_id TEXT PRIMARY KEY,
                    deliverable_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 1),
                    prior_version_ref TEXT REFERENCES mission_deliverable_versions_v2(version_id),
                    mission_version_ref TEXT NOT NULL REFERENCES coverage_mission_versions(mission_version_id),
                    mission_version_hash TEXT NOT NULL,
                    playbook_version_ref TEXT NOT NULL,
                    playbook_version_hash TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN (
                        'industry_framework','initial_screen','industry_model','company_model',
                        'forecast_lines','investment_memo','weekly_brief','event_note'
                    )),
                    subject_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deliverable_ref, version_number)
                );
                INSERT INTO mission_deliverable_versions_v2 SELECT * FROM mission_deliverable_versions;
                DROP TABLE mission_deliverable_versions;
                ALTER TABLE mission_deliverable_versions_v2 RENAME TO mission_deliverable_versions;
                CREATE INDEX IF NOT EXISTS idx_mission_deliverables_by_subject
                ON mission_deliverable_versions(mission_version_ref, subject_ref, kind, created_at);
                CREATE TRIGGER mission_deliverables_authorized_insert
                BEFORE INSERT ON mission_deliverable_versions
                WHEN dalton_mission_deliverable_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'mission deliverable insert requires MissionDeliverableAuthority'); END;
                CREATE TRIGGER mission_deliverables_no_update
                BEFORE UPDATE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                CREATE TRIGGER mission_deliverables_no_delete
                BEFORE DELETE ON mission_deliverable_versions BEGIN
                    SELECT RAISE(ABORT, 'mission deliverables are append-only'); END;
                COMMIT;
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise MissionDeliverableConflict(
                "the deliverable kind migration broke foreign keys"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("MissionDeliverableAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    # -- reads ---------------------------------------------------------------

    def live_claim_version_refs(self) -> set[str]:
        """Claim versions a document may cite: everything the Ledger holds, less retirements."""

        refs = {
            row["claim_version_id"]
            for row in self.connection.execute("SELECT claim_version_id FROM claim_versions").fetchall()
        }
        try:
            retired = {
                row["claim_version_ref"]
                for row in self.connection.execute(
                    "SELECT claim_version_ref FROM claim_retirement_decisions WHERE decision='retired'"
                ).fetchall()
            }
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            retired = set()
        return refs - retired

    def latest(self, deliverable_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM mission_deliverable_pointer p JOIN mission_deliverable_versions v "
            "ON v.version_id=p.version_id WHERE p.deliverable_ref=?",
            (_text(deliverable_ref, "deliverable_ref", maximum=512),),
        ).fetchone()
        if row is None:
            return None
        record = json.loads(row["record_json"])
        if record["content_hash"] != row["content_hash"]:
            raise MissionDeliverableConflict("mission deliverable authority drifted")
        return record

    def deliverables(
        self, mission_version_ref: str, *, kind: str | None = None, subject_ref: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT v.record_json AS record_json FROM mission_deliverable_pointer p "
            "JOIN mission_deliverable_versions v ON v.version_id=p.version_id "
            "WHERE v.mission_version_ref=?"
        )
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref", maximum=512)]
        if kind is not None:
            query += " AND v.kind=?"
            params.append(kind)
        if subject_ref is not None:
            query += " AND v.subject_ref=?"
            params.append(subject_ref)
        query += " ORDER BY v.created_at, v.version_id"
        return [json.loads(row["record_json"]) for row in self.connection.execute(query, params).fetchall()]

    # -- write ---------------------------------------------------------------

    def publish(
        self,
        *,
        kind: str,
        subject_ref: str,
        mission: Mapping[str, Any],
        playbook: Mapping[str, Any],
        template_ref: str,
        sections: Sequence[Mapping[str, Any]],
        summary: str,
        gaps: Sequence[str] = (),
        model_invocation_refs: Sequence[str] = (),
        actor_ref: str,
        idempotency_key: str | None = None,
        revision: Mapping[str, Any] | None = None,
        as_version_zero: bool = False,
        gate: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store the next version of one deliverable chain.

        ``as_version_zero`` is W3's import: this document existed before the
        system did, so it goes in at 0 and the first thing the system writes
        goes in at 1 pointing back at it. It is refused on a chain that
        already has a version, because a chain can only start once, and it
        requires ``change_reason: imported_prior``.
        """

        if kind not in DELIVERABLE_KINDS:
            raise MissionDeliverableValidationError(f"kind must be one of {list(DELIVERABLE_KINDS)}")
        subject_ref = _text(subject_ref, "subject_ref", maximum=256)
        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not (actor_ref.startswith("human:") or actor_ref.startswith("automation:")):
            raise MissionDeliverableValidationError("actor_ref must be a human: or automation: principal")
        if actor_ref.startswith("automation:"):
            if actor_ref != mission["autonomy"]["automation_principal"]:
                raise MissionDeliverableConflict("automation actor is not the mission principal")
            if WRITE_SCOPE not in mission["autonomy"]["may_write"]:
                raise MissionDeliverableConflict(
                    f"mission does not grant {WRITE_SCOPE} writes to automation"
                )
        if kind != "industry_framework" and not any(
            member["company_ref"] == subject_ref for member in mission["universe"]
        ) and subject_ref != mission["industry_ref"]:
            raise MissionDeliverableConflict("subject is not in the mission universe")
        if not isinstance(sections, Sequence) or not 1 <= len(sections) <= MAX_SECTIONS:
            raise MissionDeliverableValidationError(f"sections must be 1..{MAX_SECTIONS} entries")
        if as_version_zero:
            reason = (revision or {}).get("change_reason")
            if reason != IMPORT_CHANGE_REASON:
                raise MissionDeliverableValidationError(
                    f"a version zero must carry change_reason {IMPORT_CHANGE_REASON!r}: "
                    "it is not a revision of anything, it is where the chain starts"
                )
        elif (revision or {}).get("change_reason") == IMPORT_CHANGE_REASON:
            raise MissionDeliverableValidationError(
                f"{IMPORT_CHANGE_REASON!r} is only legal on an imported version zero"
            )
        live = self.live_claim_version_refs()
        checked = [
            validate_section(section, live_claim_refs=live, imported=as_version_zero)
            for section in sections
        ]
        if not any(section["body"] for section in checked):
            raise MissionDeliverableConflict("a deliverable with no written section is an empty shell")
        summary = _text(summary, "summary", maximum=2000)
        slug = subject_ref.rsplit(":", 1)[-1]
        deliverable_ref = f"mission-deliverable:{kind}:{slug}"
        record = {
            "schema_version": SCHEMA_VERSION,
            "deliverable_ref": deliverable_ref,
            "kind": kind,
            "subject_ref": subject_ref,
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "playbook_version_ref": playbook["id"],
            "playbook_version_hash": playbook["content_hash"],
            "template_ref": _text(template_ref, "template_ref", maximum=256),
            "summary": summary,
            "sections": checked,
            "gaps": [str(gap).strip()[:300] for gap in gaps][:40],
            "model_invocation_refs": [str(ref)[:512] for ref in model_invocation_refs][:40],
            "actor_ref": actor_ref,
            "created_at": self.clock(),
            # P14d / ADR-0008: why this version exists, when it exists because
            # somebody decided it should.  ``None`` on a first draft and on the
            # ordinary "there are new Claims" redraft, which needs no reason
            # beyond the Claims; a value here means a human checkpoint was
            # passed and names the refs that occasioned it.
            "revision": validate_revision(revision),
        }
        if gate is not None:
            # P10c's exit-gate self-assessment, stored rather than left to
            # live as one line of prose in a stage record's rationale. Out of
            # the body hash below on purpose: a gate is a reading of a
            # document, not part of it, and re-reading it must not mint a
            # version.
            record["gate"] = json.loads(json.dumps(dict(gate), ensure_ascii=False))
        # What the document says, without its version number or timestamp: an
        # unchanged body is a duplicate, not a new version.
        #
        # ``revision`` is deliberately not in the body hash.  A re-issue that
        # produces the identical document is still a duplicate, however good
        # the reason was: ADR-0008 refuses "a rewrite of an unchanged world",
        # and a change_reason is not a change.
        record["body_hash"] = content_hash({
            key: record[key] for key in (
                "kind", "subject_ref", "mission_version_ref", "mission_version_hash",
                "playbook_version_ref", "playbook_version_hash", "template_ref",
                "summary", "sections", "gaps",
            )
        })
        with self._transaction() as cur:
            pointer = cur.execute(
                "SELECT version_id, version_number, content_hash FROM mission_deliverable_pointer "
                "WHERE deliverable_ref=?", (deliverable_ref,),
            ).fetchone()
            if as_version_zero and pointer is not None:
                raise MissionDeliverableConflict(
                    "this chain already has a version; a prior document can "
                    "only be imported into a chain that has not started"
                )
            if as_version_zero:
                version, prior = 0, None
            else:
                version = 1 if pointer is None else int(pointer["version_number"]) + 1
                prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = f"mission-deliverable-version:{content_hash({'ref': deliverable_ref, 'version': version})[:32]}"
            record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            if pointer is not None:
                existing = cur.execute(
                    "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                    (pointer["version_id"],),
                ).fetchone()
                current = json.loads(existing["record_json"])
                if current.get("body_hash") == record["body_hash"]:
                    return {**current, "status": "duplicate"}
            if idempotency_key is not None:
                key = _text(idempotency_key, "idempotency_key", maximum=512)
                seen = cur.execute(
                    "SELECT version_id FROM mission_deliverable_versions WHERE deliverable_ref=? "
                    "AND json_extract(record_json,'$.idempotency_key')=?", (deliverable_ref, key),
                ).fetchone()
                if seen is not None:
                    existing = cur.execute(
                        "SELECT record_json FROM mission_deliverable_versions WHERE version_id=?",
                        (seen["version_id"],),
                    ).fetchone()
                    return {**json.loads(existing["record_json"]), "status": "duplicate"}
                record["idempotency_key"] = key
                record["content_hash"] = content_hash({k: v for k, v in record.items() if k != "content_hash"})
            cur.execute(
                "INSERT INTO mission_deliverable_versions(version_id,deliverable_ref,version_number,"
                "prior_version_ref,mission_version_ref,mission_version_hash,playbook_version_ref,"
                "playbook_version_hash,kind,subject_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], deliverable_ref, version, prior, mission["id"], mission["content_hash"],
                 playbook["id"], playbook["content_hash"], kind, subject_ref,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO mission_deliverable_pointer(deliverable_ref,version_id,version_number,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?)",
                    (deliverable_ref, record["id"], version, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE mission_deliverable_pointer SET version_id=?, version_number=?, "
                    "content_hash=?, updated_at=? WHERE deliverable_ref=?",
                    (record["id"], version, record["content_hash"], record["created_at"], deliverable_ref),
                )
        return {**record, "status": "fresh"}


__all__ = [
    "DELIVERABLE_KINDS",
    "GAP_MARKER",
    "IMPORTED_FIGURE_GAP",
    "IMPORT_CHANGE_REASON",
    "MissionDeliverableAuthority",
    "MissionDeliverableConflict",
    "MissionDeliverableError",
    "MissionDeliverableNotFound",
    "MissionDeliverableValidationError",
    "REVISION_FIELDS",
    "WRITE_SCOPE",
    "unsourced_numbers",
    "validate_revision",
    "validate_section",
    "value_tokens",
]
