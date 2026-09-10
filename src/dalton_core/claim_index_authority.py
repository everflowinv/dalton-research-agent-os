"""P12b: an append-only index over Claims that never touches a Claim.

A Claim in this Ledger is immutable and its contract is frozen (ClaimVersion
0.2).  Everything the analyst blueprint asks for -- what the claim is about,
when it is about, how much its source is worth, whether it is one of three
copies of the same fact -- is a *judgement about* a claim, not a property of
it.  Putting those on the claim would mean editing history every time the
judgement improved.

So they live here, in their own versioned authority, keyed by
``claim_version_ref``.  The rules are the ones every authority in this
repository follows: append-only, content-hashed, three ``dalton_authorized()``
triggers, a version chain, a read-back after the write, and ``duplicate``
rather than a new version when nothing changed.

Two things are deliberately *not* here.

``status`` is not stored.  It is projected from adjudications by
``DaltonStore.project_claim_status`` and a stored copy would be a second answer
to a question that already has one -- the exact mistake ClaimIndex 0.1 fixed
when it stopped accepting caller-supplied bundles.

The tagging *rules* are not here either; they are in ``claim_index_tagging``.
This module holds the contract and the store: what an entry is allowed to say,
and how saying it is recorded.  A rule change is then a change to what gets
written, and the chain shows it happening, which is the point.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claim_aspect_vocabulary import require_aspect
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"

_SCHEMA_PATH = Path(__file__).with_name("claim_index_schema.sql")

# How much the source of a claim is worth, strongest first.  Derived from the
# document the claim came from -- never from the text of the claim, and never
# asked of a model.  A model asked "how reliable is this" answers about its own
# confidence, which is a different question with the same shape.
#
#   filing               the company published it (10-K/10-Q/press release,
#                        or SEC XBRL through the statements lane)
#   management_statement management said it (earnings call, IR event)
#   internal_prior       this fund wrote it, earlier -- an old Initial Screen,
#                        a memo, a maintained model (W3)
#   sell_side            a broker wrote it
#   news                 a public-web page said it
#   other                the provenance chain does not reach a graded document
#
# ``internal_prior`` sits above the sell side and below management for the
# reason the owner gave when they asked for it: our own earlier work is often
# the best thing in the building and is still not the company speaking. It is
# also the only tier with an age rule -- see ``STALE_AFTER_DAYS`` in
# ``claim_index_tagging`` -- because it is the only tier whose whole meaning is
# "this is what we thought", and what we thought two years ago about a company
# that has filed eight quarters since is a different kind of claim.
IMPORTANCE_TIERS: tuple[str, ...] = (
    "filing", "management_statement", "internal_prior", "sell_side", "news",
    "other",
)
IMPORTANCE_RANK: Mapping[str, int] = {
    tier: index for index, tier in enumerate(IMPORTANCE_TIERS)
}

# What ``as_of`` was read off.  A date with no basis is a date nobody can
# check; "2026-09-08" meaning "we fetched the page that day" and the same
# string meaning "the quarter this is about ended that day" are not the same
# fact and must never be compared as if they were.
AS_OF_BASES: tuple[str, ...] = (
    # the claim's period is a structured period and this is its end
    "period_end",
    # the claim's period is a label ("Q2 2026", "FY2025") this parsed
    "period_label",
    # the producing document says when it was published
    "document_published_at",
    # nothing else was available; this is when the evidence was retrieved
    "evidence_retrieved_at",
    # no date at all
    "unknown",
)

ASPECT_SOURCES: tuple[str, ...] = ("rule", "model")
REVISION_REASONS: tuple[str, ...] = ("tagged", "recanonicalised")

# W4 / Chem retrospective §7.2: *what kind of quantity* a number is, which is a
# different question from ``importance`` above.  ``importance`` says who said
# it; this says what it is.  The Chem review's sharpest single finding is that
# the two get conflated: an MDI--benzene spread published by a price reporting
# agency is a perfectly reliable public number that is *not* the cash margin of
# a plant, and a system that files it beside a filed figure has quietly told
# itself the company earned it.
#
#   company_figure       the company published this quantity for itself
#   market_proxy         a public quantity that stands in for one the company
#                        does not publish, and sits at a distance from what the
#                        company realises: spreads, list prices, futures
#                        continuations, industry operating rates
#   third_party_estimate somebody outside the company forecast or estimated it
#                        -- a broker, a consensus, an industry consultancy
#   own_assumption       this fund decided it; nobody published it at all
#   qualitative          a statement rather than a quantity
#
# ``market_proxy`` is the one with a rule attached, and it is deliberately the
# only one: a forecast assumption that cites a market proxy must say how far
# that proxy sits from the company's realised figure (``proxy_gap``), or the
# assumption is refused whole.  See ``model_forecast_driver.REF_KINDS``.
EVIDENCE_KINDS: tuple[str, ...] = (
    "company_figure",
    "market_proxy",
    "third_party_estimate",
    "own_assumption",
    "qualitative",
)
EVIDENCE_KIND_DEFINITIONS: Mapping[str, str] = {
    "company_figure": (
        "the company published this quantity for itself, in a filing, a press "
        "release or a disclosed operating metric"
    ),
    "market_proxy": (
        "a public quantity standing in for one the company does not publish -- "
        "a spread, a list price, a futures continuation, an industry operating "
        "rate -- which sits at a stated distance from what the company realises"
    ),
    "third_party_estimate": (
        "somebody outside the company estimated or forecast it: a broker, a "
        "consensus, an industry consultancy"
    ),
    "own_assumption": (
        "this fund decided it; no source outside this building published it"
    ),
    "qualitative": "a statement about the business rather than a quantity",
}
#: The evidence kind that may not be cited without saying how far it is from
#: the company's own realised figure.
MARKET_PROXY = "market_proxy"

_ENTRY_FIELDS = frozenset({
    "schema_version", "id", "created_at", "entry_ref", "version",
    "prior_version_ref", "claim_version_ref", "claim_version_hash", "claim_ref",
    "claim_created_at", "subject_ref", "metric_or_aspect", "period_key",
    "claim_kind", "aspect", "aspect_source", "as_of", "as_of_basis",
    "importance", "importance_basis", "dedupe_group_ref", "dedupe_group_key",
    "is_canonical", "revision_reason", "tagger_ref", "tagger_hash",
    "actor_ref", "content_hash",
})

# The part of an entry that decides whether re-recording it is a new version or
# a duplicate.  Identity, ordering and the clock are excluded on purpose: the
# same judgement recorded twice is one judgement.  ``is_canonical`` is excluded
# too, because it is a fact about the group rather than about this claim, and
# it is reconciled separately -- otherwise a claim joining a group would make
# every other member's tag look "changed".
_BINDING_FIELDS = (
    "claim_version_ref", "claim_version_hash", "claim_ref", "claim_created_at",
    "subject_ref", "metric_or_aspect", "period_key", "claim_kind", "aspect",
    "aspect_source", "as_of", "as_of_basis", "importance", "importance_basis",
    "dedupe_group_ref", "dedupe_group_key", "tagger_ref", "tagger_hash",
    "actor_ref",
)

TABLE = "claim_index_entry_versions"


class ClaimIndexError(RuntimeError):
    """Base error for the Claim index authority."""


class ClaimIndexValidationError(ClaimIndexError, ValueError):
    """An entry does not satisfy the closed contract."""


class ClaimIndexConflict(ClaimIndexError):
    """Stored bytes disagree with themselves or with the request."""


class ClaimIndexNotFound(ClaimIndexError, LookupError):
    """No such entry."""


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClaimIndexValidationError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _hash(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ClaimIndexValidationError(f"{name} must be lowercase SHA-256")
    return value


def _one_of(value: Any, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ClaimIndexValidationError(
            f"{name} must be one of {', '.join(allowed)}; got {value!r}"
        )
    return str(value)


def _iso_date(value: Any, name: str) -> str | None:
    if value is None:
        return None
    value = _text(value, name)
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ClaimIndexValidationError(f"{name} must be YYYY-MM-DD") from exc
    if len(value) != 10:
        raise ClaimIndexValidationError(f"{name} must be YYYY-MM-DD")
    return value


def group_ref_for(key: str) -> str:
    """The stable ref of a dedupe group, derived only from its key.

    Content-addressed so that two runs, two processes and two Cores that
    compute the same key agree on the group without co-ordinating.
    """

    return "claim-dedupe-group:" + content_hash({"key": _text(key, "dedupe_group_key")})[:32]


def entry_ref_for(claim_version_ref: str) -> str:
    """One entry per claim version, named after it."""

    return "claim-index-entry:" + content_hash(
        {"claim_version_ref": _text(claim_version_ref, "claim_version_ref")}
    )[:32]


def validate_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    """The closed shape of one ClaimIndexEntryVersion."""

    if not isinstance(value, Mapping):
        raise ClaimIndexValidationError("ClaimIndexEntryVersion must be an object")
    wire = dict(value)
    if set(wire) != _ENTRY_FIELDS:
        raise ClaimIndexValidationError(
            "ClaimIndexEntryVersion has invalid closed shape; "
            f"missing={sorted(_ENTRY_FIELDS - set(wire))}, "
            f"unknown={sorted(set(wire) - _ENTRY_FIELDS)}"
        )
    if wire["schema_version"] != SCHEMA_VERSION:
        raise ClaimIndexValidationError(
            "unsupported ClaimIndexEntryVersion schema_version"
        )
    for field in (
        "id", "created_at", "entry_ref", "claim_version_ref", "claim_ref",
        "claim_created_at", "subject_ref", "metric_or_aspect", "period_key",
        "importance_basis", "dedupe_group_ref", "dedupe_group_key",
        "tagger_ref", "actor_ref",
    ):
        wire[field] = _text(wire[field], field)
    wire["claim_version_hash"] = _hash(wire["claim_version_hash"], "claim_version_hash")
    wire["tagger_hash"] = _hash(wire["tagger_hash"], "tagger_hash")
    if not isinstance(wire["version"], int) or isinstance(wire["version"], bool) \
            or wire["version"] < 1:
        raise ClaimIndexValidationError("version must be a positive integer")
    wire["prior_version_ref"] = _optional_text(
        wire["prior_version_ref"], "prior_version_ref"
    )
    if wire["claim_kind"] not in {"quantitative", "qualitative"}:
        raise ClaimIndexValidationError(
            "claim_kind must be quantitative or qualitative"
        )
    wire["aspect"] = require_aspect(wire["aspect"])
    wire["aspect_source"] = _one_of(wire["aspect_source"], ASPECT_SOURCES, "aspect_source")
    wire["as_of"] = _iso_date(wire["as_of"], "as_of")
    wire["as_of_basis"] = _one_of(wire["as_of_basis"], AS_OF_BASES, "as_of_basis")
    if (wire["as_of"] is None) != (wire["as_of_basis"] == "unknown"):
        raise ClaimIndexValidationError(
            "as_of and as_of_basis must agree: a date needs a basis and "
            "'unknown' needs no date"
        )
    wire["importance"] = _one_of(wire["importance"], IMPORTANCE_TIERS, "importance")
    if wire["dedupe_group_ref"] != group_ref_for(wire["dedupe_group_key"]):
        raise ClaimIndexConflict("dedupe_group_ref is not derived from its key")
    if wire["entry_ref"] != entry_ref_for(wire["claim_version_ref"]):
        raise ClaimIndexConflict("entry_ref is not derived from the claim version")
    if not isinstance(wire["is_canonical"], bool):
        raise ClaimIndexValidationError("is_canonical must be a boolean")
    wire["revision_reason"] = _one_of(
        wire["revision_reason"], REVISION_REASONS, "revision_reason"
    )
    body = {key: item for key, item in wire.items() if key != "content_hash"}
    expected = content_hash(body)
    if wire["content_hash"] != expected:
        raise ClaimIndexConflict("ClaimIndexEntryVersion content hash drifted")
    return wire


def _decode(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise ClaimIndexNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise ClaimIndexConflict(f"{name} record_json is invalid") from exc
    if not isinstance(wire, dict) or canonical_json(wire) != row["record_json"]:
        raise ClaimIndexConflict(f"{name} record_json is not canonical")
    entry = validate_entry(wire)
    if entry["content_hash"] != row["content_hash"] or entry["id"] != row["version_id"]:
        raise ClaimIndexConflict(f"{name} identity columns drifted")
    columns = {
        "entry_ref": entry["entry_ref"],
        "version_number": entry["version"],
        "prior_version_id": entry["prior_version_ref"],
        "claim_version_ref": entry["claim_version_ref"],
        "aspect": entry["aspect"],
        "as_of": entry["as_of"],
        "importance": entry["importance"],
        "dedupe_group_ref": entry["dedupe_group_ref"],
        "is_canonical": int(entry["is_canonical"]),
        "actor_ref": entry["actor_ref"],
        "created_at": entry["created_at"],
    }
    keys = set(row.keys())
    for column, expected in columns.items():
        if column in keys and row[column] != expected:
            raise ClaimIndexConflict(f"{name} column {column} drifted")
    return entry


def table_exists(connection: Any) -> bool:
    """Whether this Core has ever opened the index.

    Readers ask before they join.  A Core written before P12b has no index and
    must keep answering exactly as it did -- degrading to "no index" is the
    behaviour, not an error.
    """

    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def current_entries(
    connection: Any,
    *,
    claim_version_refs: Sequence[str] | None = None,
    subject_ref: str | None = None,
) -> dict[str, dict[str, Any]]:
    """The current entry per claim version, keyed by ``claim_version_ref``.

    Read-only and safe on a Core with no index: an absent table is an empty
    answer rather than a raised error, because "this Core predates the index"
    is a normal state and every reader has to survive it.
    """

    if not table_exists(connection):
        return {}
    query = (
        f"SELECT v.* FROM {TABLE} v WHERE v.version_number = "
        f"(SELECT MAX(x.version_number) FROM {TABLE} x WHERE x.entry_ref=v.entry_ref)"
    )
    params: list[Any] = []
    if subject_ref is not None:
        query += " AND v.subject_ref=?"
        params.append(_text(subject_ref, "subject_ref"))
    rows = connection.execute(query, params).fetchall()
    wanted = None if claim_version_refs is None else set(claim_version_refs)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if wanted is not None and row["claim_version_ref"] not in wanted:
            continue
        entry = _decode(row, f"ClaimIndexEntryVersion {row['version_id']}")
        result[entry["claim_version_ref"]] = entry
    return result


def canonical_order_key(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    """How one member of a dedupe group beats another.

    Highest importance, then latest ``as_of``, then the earliest claim -- in
    that order and no other.  The last rule is what stops the group from
    flipping every time an equally good duplicate arrives: the first one to say
    it keeps the citation, which is also what a reader expects when three
    identical revenue Claims are cited side by side.
    """

    as_of = entry.get("as_of")
    return (
        IMPORTANCE_RANK[entry["importance"]],
        # None sorts last among dates; a dated claim beats an undated one.
        (1, "") if as_of is None else (0, _invert_date(as_of)),
        entry["claim_created_at"],
        entry["claim_version_ref"],
    )


def _invert_date(value: str) -> str:
    """Sort dates descending inside an ascending tuple sort."""

    return "".join(chr(ord("9") - (ord(ch) - ord("0"))) if ch.isdigit() else ch
                   for ch in value)


class ClaimIndexAuthority:
    """Append-only ClaimIndexEntryVersion authority over one Core."""

    def __init__(self, store: Any) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("ClaimIndexAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._widen_importance_check()

    def _widen_importance_check(self) -> None:
        """W3: admit ``internal_prior`` on a Core built before that tier existed.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that is already
        there, so a Core created under the five-tier CHECK would keep refusing
        the sixth for ever -- and it would refuse it with an ``IntegrityError``
        from the constraint rather than with the readable message the Python
        vocabulary check gives, which is the failure mode that makes this worth
        a rebuild. Follows ``MissionDeliverableAuthority._widen_kind_check``
        exactly, including the foreign-key check afterwards.
        """

        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            f"AND name='{TABLE}'"
        ).fetchone()
        if row is None or "'internal_prior'" in (row["sql"] or ""):
            return
        if self.connection.in_transaction:
            raise ClaimIndexConflict(
                "the claim index importance migration requires no open transaction"
            )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS claim_index_entry_insert_guard;
                DROP TRIGGER IF EXISTS claim_index_entry_no_update;
                DROP TRIGGER IF EXISTS claim_index_entry_no_delete;
                CREATE TABLE claim_index_entry_versions_v2 (
                    version_id TEXT PRIMARY KEY,
                    entry_ref TEXT NOT NULL,
                    version_number INTEGER NOT NULL CHECK(version_number >= 1),
                    prior_version_id TEXT REFERENCES claim_index_entry_versions_v2(version_id),
                    claim_version_ref TEXT NOT NULL,
                    claim_version_hash TEXT NOT NULL,
                    claim_ref TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    aspect TEXT NOT NULL,
                    as_of TEXT,
                    as_of_basis TEXT NOT NULL,
                    importance TEXT NOT NULL CHECK(importance IN (
                        'filing', 'management_statement', 'internal_prior',
                        'sell_side', 'news', 'other')),
                    dedupe_group_ref TEXT NOT NULL,
                    is_canonical INTEGER NOT NULL CHECK(is_canonical IN (0, 1)),
                    tagger_ref TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    actor_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(entry_ref, version_number)
                );
                INSERT INTO claim_index_entry_versions_v2
                SELECT * FROM claim_index_entry_versions;
                DROP TABLE claim_index_entry_versions;
                ALTER TABLE claim_index_entry_versions_v2
                RENAME TO claim_index_entry_versions;
                CREATE INDEX IF NOT EXISTS claim_index_entries_by_claim
                ON claim_index_entry_versions(claim_version_ref, version_number);
                CREATE INDEX IF NOT EXISTS claim_index_entries_by_subject
                ON claim_index_entry_versions(subject_ref, aspect, as_of);
                CREATE INDEX IF NOT EXISTS claim_index_entries_by_group
                ON claim_index_entry_versions(dedupe_group_ref, version_number);
                CREATE TRIGGER claim_index_entry_insert_guard
                BEFORE INSERT ON claim_index_entry_versions
                WHEN dalton_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'claim index entry insert requires DaltonStore');
                END;
                CREATE TRIGGER claim_index_entry_no_update
                BEFORE UPDATE ON claim_index_entry_versions BEGIN
                    SELECT RAISE(ABORT, 'claim index entries are immutable');
                END;
                CREATE TRIGGER claim_index_entry_no_delete
                BEFORE DELETE ON claim_index_entry_versions BEGIN
                    SELECT RAISE(ABORT, 'claim index entries are immutable');
                END;
                COMMIT;
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ClaimIndexConflict(
                "the claim index importance migration broke foreign keys"
            )

    # -- reads ------------------------------------------------------------

    def entry(self, version_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE version_id=?",
            (_text(version_ref, "version_ref"),),
        ).fetchone()
        return _decode(row, f"ClaimIndexEntryVersion {version_ref}")

    def _latest_row(self, entry_ref: str, cursor: Any | None = None) -> sqlite3.Row | None:
        connection = self.connection if cursor is None else cursor
        return connection.execute(
            f"SELECT * FROM {TABLE} WHERE entry_ref=? "
            "ORDER BY version_number DESC LIMIT 1",
            (entry_ref,),
        ).fetchone()

    def current_entry(self, claim_version_ref: str) -> dict[str, Any] | None:
        row = self._latest_row(entry_ref_for(claim_version_ref))
        if row is None:
            return None
        return _decode(row, f"ClaimIndexEntryVersion for {claim_version_ref}")

    def entries(
        self,
        *,
        subject_ref: str | None = None,
        claim_version_refs: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        return current_entries(
            self.connection, subject_ref=subject_ref,
            claim_version_refs=claim_version_refs,
        )

    def group_members(self, dedupe_group_ref: str) -> list[dict[str, Any]]:
        """Every current entry in one dedupe group, canonical first."""

        rows = self.connection.execute(
            f"SELECT v.* FROM {TABLE} v WHERE v.dedupe_group_ref=? AND "
            f"v.version_number=(SELECT MAX(x.version_number) FROM {TABLE} x "
            "WHERE x.entry_ref=v.entry_ref)",
            (_text(dedupe_group_ref, "dedupe_group_ref"),),
        ).fetchall()
        members = [_decode(row, "ClaimIndexEntryVersion") for row in rows]
        members.sort(key=canonical_order_key)
        return members

    def versions(self, claim_version_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT * FROM {TABLE} WHERE entry_ref=? ORDER BY version_number",
            (entry_ref_for(claim_version_ref),),
        ).fetchall()
        return [_decode(row, "ClaimIndexEntryVersion") for row in rows]

    def counts(self) -> dict[str, int]:
        entries = self.connection.execute(
            f"SELECT COUNT(DISTINCT entry_ref) FROM {TABLE}"
        ).fetchone()[0]
        versions = self.connection.execute(
            f"SELECT COUNT(*) FROM {TABLE}"
        ).fetchone()[0]
        return {"entries": int(entries), "versions": int(versions)}

    # -- the write --------------------------------------------------------

    def record_entry(
        self,
        *,
        claim_version_ref: str,
        claim_version_hash: str,
        claim_ref: str,
        claim_created_at: str,
        subject_ref: str,
        metric_or_aspect: str,
        period_key: str,
        claim_kind: str,
        aspect: str,
        aspect_source: str,
        as_of: str | None,
        as_of_basis: str,
        importance: str,
        importance_basis: str,
        dedupe_group_key: str,
        tagger_ref: str,
        tagger_hash: str,
        actor_ref: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Record one tag, then settle its dedupe group.

        Returns ``{"status": "fresh"|"duplicate", ..., "recanonicalised": [...]}``.
        The whole thing is one transaction: an entry that joined a group
        without the group being re-settled would leave two canonical members,
        and a reader asking for canonical-only would get the same fact twice --
        which is the entire complaint this index exists to answer.
        """

        entry_ref = entry_ref_for(claim_version_ref)
        group_key = _text(dedupe_group_key, "dedupe_group_key")
        body = {
            "claim_version_ref": _text(claim_version_ref, "claim_version_ref"),
            "claim_version_hash": _hash(claim_version_hash, "claim_version_hash"),
            "claim_ref": _text(claim_ref, "claim_ref"),
            "claim_created_at": _text(claim_created_at, "claim_created_at"),
            "subject_ref": _text(subject_ref, "subject_ref"),
            "metric_or_aspect": _text(metric_or_aspect, "metric_or_aspect"),
            "period_key": _text(period_key, "period_key"),
            "claim_kind": claim_kind,
            "aspect": require_aspect(aspect),
            "aspect_source": _one_of(aspect_source, ASPECT_SOURCES, "aspect_source"),
            "as_of": _iso_date(as_of, "as_of"),
            "as_of_basis": _one_of(as_of_basis, AS_OF_BASES, "as_of_basis"),
            "importance": _one_of(importance, IMPORTANCE_TIERS, "importance"),
            "importance_basis": _text(importance_basis, "importance_basis"),
            "dedupe_group_ref": group_ref_for(group_key),
            "dedupe_group_key": group_key,
            "tagger_ref": _text(tagger_ref, "tagger_ref"),
            "tagger_hash": _hash(tagger_hash, "tagger_hash"),
            "actor_ref": _text(actor_ref, "actor_ref"),
        }
        created_at = _text(created_at, "created_at")

        latest_row = self._latest_row(entry_ref)
        latest = (
            None if latest_row is None
            else _decode(latest_row, f"ClaimIndexEntryVersion for {claim_version_ref}")
        )
        unchanged = latest is not None and all(
            latest[field] == body[field] for field in _BINDING_FIELDS
        )
        if unchanged:
            # An unchanged tag is one judgement, not two -- but the group it
            # sits in may still be wrong, because a *different* claim may have
            # joined or left it since. Settling is not the same question as
            # recording, and short-circuiting the write must not short-circuit
            # the settlement.
            recanonicalised = self._settle_only(body["dedupe_group_ref"], created_at)
            current = self.current_entry(body["claim_version_ref"]) or latest
            return {"status": "duplicate", "recanonicalised": recanonicalised, **current}

        # A re-tag can move a claim from one group to another -- a period that
        # now parses, a basis that was missing. The group it leaves has to be
        # settled too, or the entry it displaced stays non-canonical forever
        # with nothing canonical above it.
        vacated = (
            latest["dedupe_group_ref"]
            if latest is not None and latest["dedupe_group_ref"] != body["dedupe_group_ref"]
            else None
        )
        recanonicalised: list[str] = []
        with self.store._transaction() as cur:
            version = 1 if latest is None else latest["version"] + 1
            prior = None if latest is None else latest["id"]
            wire = self._write(
                cur, body, version=version, prior_version_ref=prior,
                # Canonicality is settled below, once this entry is in the
                # group; writing "True" first and correcting would leave a
                # version chain full of the authority arguing with itself.
                is_canonical=self._would_be_canonical(cur, body, entry_ref),
                revision_reason="tagged", created_at=created_at,
            )
            recanonicalised = self._settle_group(
                cur, body["dedupe_group_ref"], skip_entry_ref=entry_ref,
                created_at=created_at,
            )
            if vacated is not None:
                recanonicalised += self._settle_group(
                    cur, vacated, skip_entry_ref=entry_ref, created_at=created_at,
                )
        stored = self.entry(wire["id"])
        if stored["content_hash"] != wire["content_hash"]:
            raise ClaimIndexConflict("stored ClaimIndexEntryVersion did not read back")
        return {"status": "fresh", "recanonicalised": recanonicalised, **stored}

    def settle_group(self, dedupe_group_ref: str, *, created_at: str) -> list[str]:
        """Re-settle one group without recording anything about a claim.

        Public because a retirement, a retraction or a rule change can change
        who should be canonical without any entry being re-tagged, and there
        has to be a way to say so that is not "write the same tag again".
        """

        return self._settle_only(_text(dedupe_group_ref, "dedupe_group_ref"), created_at)

    def _settle_only(self, dedupe_group_ref: str, created_at: str) -> list[str]:
        if not self._group_rows(self.connection, dedupe_group_ref):
            return []
        with self.store._transaction() as cur:
            return self._settle_group(
                cur, dedupe_group_ref, skip_entry_ref=None, created_at=created_at,
            )

    # -- internals --------------------------------------------------------

    def _write(
        self,
        cur: Any,
        body: Mapping[str, Any],
        *,
        version: int,
        prior_version_ref: str | None,
        is_canonical: bool,
        revision_reason: str,
        created_at: str,
    ) -> dict[str, Any]:
        entry_ref = entry_ref_for(body["claim_version_ref"])
        identity = {
            "entry_ref": entry_ref, "version": version,
            "prior_version_ref": prior_version_ref,
            "is_canonical": is_canonical,
            "revision_reason": revision_reason,
            **dict(body),
        }
        wire = {
            "schema_version": SCHEMA_VERSION,
            "id": "claim-index-entry-version:" + content_hash(identity)[:32],
            "created_at": created_at,
            "entry_ref": entry_ref,
            "version": version,
            "prior_version_ref": prior_version_ref,
            "is_canonical": is_canonical,
            "revision_reason": revision_reason,
            **dict(body),
        }
        wire["content_hash"] = content_hash(wire)
        wire = validate_entry(wire)
        cur.execute(
            f"INSERT INTO {TABLE}(version_id,entry_ref,version_number,prior_version_id,"
            "claim_version_ref,claim_version_hash,claim_ref,subject_ref,aspect,as_of,"
            "as_of_basis,importance,dedupe_group_ref,is_canonical,tagger_ref,"
            "record_json,content_hash,actor_ref,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wire["id"], wire["entry_ref"], wire["version"], wire["prior_version_ref"],
                wire["claim_version_ref"], wire["claim_version_hash"], wire["claim_ref"],
                wire["subject_ref"], wire["aspect"], wire["as_of"], wire["as_of_basis"],
                wire["importance"], wire["dedupe_group_ref"], int(wire["is_canonical"]),
                wire["tagger_ref"], canonical_json(wire), wire["content_hash"],
                wire["actor_ref"], wire["created_at"],
            ),
        )
        return wire

    def _group_rows(self, cur: Any, dedupe_group_ref: str) -> list[dict[str, Any]]:
        rows = cur.execute(
            f"SELECT v.* FROM {TABLE} v WHERE v.dedupe_group_ref=? AND "
            f"v.version_number=(SELECT MAX(x.version_number) FROM {TABLE} x "
            "WHERE x.entry_ref=v.entry_ref)",
            (dedupe_group_ref,),
        ).fetchall()
        return [_decode(row, "ClaimIndexEntryVersion") for row in rows]

    def _would_be_canonical(
        self, cur: Any, body: Mapping[str, Any], entry_ref: str
    ) -> bool:
        members = [
            item for item in self._group_rows(cur, body["dedupe_group_ref"])
            if item["entry_ref"] != entry_ref
        ]
        mine = canonical_order_key(body)
        return all(mine < canonical_order_key(other) for other in members)

    def _settle_group(
        self, cur: Any, dedupe_group_ref: str, *, skip_entry_ref: str | None,
        created_at: str,
    ) -> list[str]:
        """Re-version any member whose canonicality the new entry changed."""

        members = self._group_rows(cur, dedupe_group_ref)
        if not members:
            return []
        winner = min(members, key=canonical_order_key)
        changed: list[str] = []
        for member in members:
            expected = member["id"] == winner["id"]
            if member["is_canonical"] == expected or member["entry_ref"] == skip_entry_ref:
                continue
            body = {field: member[field] for field in _BINDING_FIELDS}
            wire = self._write(
                cur, body, version=member["version"] + 1,
                prior_version_ref=member["id"], is_canonical=expected,
                revision_reason="recanonicalised", created_at=created_at,
            )
            changed.append(wire["id"])
        return changed


__all__ = [
    "AS_OF_BASES",
    "ASPECT_SOURCES",
    "EVIDENCE_KINDS",
    "EVIDENCE_KIND_DEFINITIONS",
    "IMPORTANCE_RANK",
    "IMPORTANCE_TIERS",
    "MARKET_PROXY",
    "REVISION_REASONS",
    "SCHEMA_VERSION",
    "TABLE",
    "ClaimIndexAuthority",
    "ClaimIndexConflict",
    "ClaimIndexError",
    "ClaimIndexNotFound",
    "ClaimIndexValidationError",
    "canonical_order_key",
    "current_entries",
    "entry_ref_for",
    "group_ref_for",
    "table_exists",
    "validate_entry",
]
