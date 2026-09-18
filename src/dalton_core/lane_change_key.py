"""B1-5: the cheap question a lane asks before it pays for the expensive one.

Two lanes -- the company dossier and the debate map -- decide what to do by
fingerprinting a subject's evidence.  Both fingerprints are honest and both
are expensive: they materialise the subject's Claim rows, annotate them and
hash the result.  On the live Core that is 9.7 s and 8.0 s of the writer's
single store thread *every tick*, for five companies of which, measured over
two days, exactly one gains a Claim in a given window.  The other four pay the
full price to learn that nothing moved.

A change key is the cheap half of that question.  It is a handful of SQL
aggregates -- counts, the newest row id, the head version of each chain --
over precisely the tables the fingerprint reads, and it has one property that
makes it safe to gate on: it moves whenever the fingerprint could.  It may
move when the fingerprint would not (a Claim for another company landing in a
table this key only aggregates globally), and that costs one recomputation;
it may never fail to move, because that would freeze a subject.

So the keys are deliberately built from *appends*.  Every table here is
append-only under the authority's triggers, which is why ``COUNT(*)`` plus the
newest identifier is enough: there is no update to miss and no delete to miss.
A table this module has never heard of contributes nothing, which is why the
per-lane key composers below, not this module, decide which tables belong to
which fingerprint -- an over-broad key is a lane that recomputes for another
lane's writes, and that is how a gate stops being a gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Tables whose per-subject aggregate is grouped on a column, not on JSON.
_ABSENT = "-"


def _absent_table(exc: BaseException) -> bool:
    return "no such table" in str(exc)


def _grouped(
    connection: Any, sql: str, params: Iterable[Any] = (), *,
    optional: bool = False,
) -> dict[str, str]:
    """``{group -> "a|b|c"}`` for an aggregate query.

    An ``optional`` table this Core has never opened answers "this input does
    not exist here", which is a stable key rather than an error: the
    fingerprint reading it makes the same choice.

    Every other failure is raised, and that is the whole safety argument.  A
    read that quietly returned nothing would produce a key that no longer
    moves when the thing it was aggregating does, and a key that stops moving
    freezes the subject it gates -- silently, and for as long as the fault
    lasts.  The caller catches this and recomputes everything, which is what
    the lane did before the gate existed and is always safe.
    """

    try:
        rows = connection.execute(sql, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        if optional and _absent_table(exc):
            return {}
        raise
    keys: dict[str, str] = {}
    for row in rows:
        values = list(row)
        group = values[0]
        if group is None:
            continue
        keys[str(group)] = "|".join(
            _ABSENT if value is None else str(value) for value in values[1:]
        )
    return keys


def _scalar(connection: Any, sql: str, *, optional: bool = False) -> str:
    try:
        row = connection.execute(sql).fetchone()
    except sqlite3.Error as exc:
        if optional and _absent_table(exc):
            return "absent"
        raise
    if row is None:
        return "absent"
    return "|".join(_ABSENT if value is None else str(value) for value in row)


def claim_change_keys(connection: Any) -> dict[str, str]:
    """Per-subject Claim aggregate: how many, and which is newest.

    ``subject_ref`` lives inside ``claim_json``, so this is one grouped scan
    with a JSON extraction rather than an index seek.  It is still two orders
    of magnitude cheaper than the fingerprint it gates -- 0.05 s warm against
    5 s -- because it parses one field instead of building every row, and one
    scan answers for every company at once rather than once per company.
    """

    return _grouped(
        connection,
        "SELECT json_extract(claim_json,'$.subject_ref') AS subject, "
        "COUNT(*) AS n, MAX(claim_version_id) AS newest FROM claim_versions "
        "GROUP BY subject",
        # Not optional: every Core has a Ledger, so a failure here is a fault
        # and not a shape, and a Claim aggregate that silently went missing
        # would stop the key moving when Claims land.
    )


def claim_index_change_keys(connection: Any) -> dict[str, str]:
    """Per-subject index aggregate.  ``subject_ref`` is a column and indexed."""

    return _grouped(
        connection,
        "SELECT subject_ref, COUNT(*) AS n, MAX(version_id) AS newest "
        "FROM claim_index_entry_versions GROUP BY subject_ref",
        optional=True,
    )


def head_change_keys(
    connection: Any, table: str, subject_column: str,
) -> dict[str, str]:
    """Per-subject head of a version chain: how many versions and the newest."""

    return _grouped(
        connection,
        f"SELECT {subject_column}, COUNT(*) AS n, MAX(version_number) AS v, "
        f"MAX(version_id) AS newest FROM {table} GROUP BY {subject_column}",
        optional=True,
    )


def document_figure_change_keys(connection: Any) -> dict[str, str]:
    return _grouped(
        connection,
        "SELECT company_ref, COUNT(*) AS n, MAX(content_hash) AS newest "
        "FROM coverage_mission_document_figures GROUP BY company_ref",
        optional=True,
    )


def statement_change_keys(connection: Any) -> dict[str, str]:
    """Per-company filed-statement aggregate.

    Grouped on the filing, whose ``company_ref`` is the only place the company
    appears; the lines hang off ``ingest_id`` and are covered globally by
    ``append_probe`` because a line cannot arrive without its filing being
    touched, and the global probe catches the case where it does anyway.
    """

    return _grouped(
        connection,
        "SELECT company_ref, COUNT(*) AS n, MAX(ingest_id) AS newest "
        "FROM coverage_mission_statement_filings GROUP BY company_ref",
        optional=True,
    )


def append_probe(connection: Any, table: str) -> str:
    """A whole table's append state: how many rows and the newest row id.

    For the small supporting tables whose rows carry no subject -- retirement
    decisions, evidence relations -- where a per-subject key would cost more
    than the recomputation it saves.  Conservative on purpose: a retirement
    for one company re-fingerprints all five, and there have been 155 of them
    in the Core's lifetime.
    """

    return _scalar(connection, f"SELECT COUNT(*), MAX(rowid) FROM {table}",
                   optional=True)


def compose(parts: Iterable[Any]) -> str:
    """One short key from the pieces, stable across processes."""

    material = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class ChangeKeyMemo:
    """Last change key and the signature computed under it, per subject.

    In memory for the tick-to-tick case and on disk for the restart case.
    Without the file a deploy -- which happens far more often than five
    companies gaining Claims in the same five minutes -- makes the first tick
    after start recompute every subject at once, which is the exact 30 s
    timeout this gate exists to remove.

    The file is a cache and is treated as one: an unreadable or corrupt file
    is an empty memo, never an error, because the only cost of losing it is
    one expensive tick.
    """

    def __init__(self, path: Any | None) -> None:
        self.path = None if path is None else Path(path)
        self._entries: dict[str, tuple[str, str]] = {}
        self._loaded = False

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, Mapping):
            return
        for subject, entry in (raw.get("subjects") or {}).items():
            if not isinstance(entry, Mapping):
                continue
            key, signature = entry.get("key"), entry.get("signature")
            if isinstance(key, str) and isinstance(signature, str):
                self._entries[str(subject)] = (key, signature)

    def _persist(self) -> None:
        if self.path is None:
            return
        payload = {
            "schema_version": "0.1",
            "subjects": {
                subject: {"key": key, "signature": signature}
                for subject, (key, signature) in sorted(self._entries.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=f".{self.path.name}.")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError:
            # A cache that cannot be written is a cache that is not there.
            return

    # -- the gate ----------------------------------------------------------

    def cached(self, subject: str, key: str | None) -> str | None:
        """The signature already computed under this exact key, or ``None``."""

        if key is None:
            return None
        self._load()
        entry = self._entries.get(str(subject))
        if entry is None or entry[0] != key:
            return None
        return entry[1]

    def remember(self, subject: str, key: str | None, signature: str) -> None:
        if key is None:
            return
        self._load()
        subject = str(subject)
        if self._entries.get(subject) == (key, signature):
            return
        self._entries[subject] = (key, signature)
        self._persist()

    def forget(self, subjects: Iterable[str]) -> None:
        """Drop subjects nobody asks about any more (a mission that shrank)."""

        self._load()
        wanted = {str(subject) for subject in subjects}
        stale = set(self._entries) - wanted
        if not stale:
            return
        for subject in stale:
            self._entries.pop(subject, None)
        self._persist()

    def snapshot(self) -> dict[str, tuple[str, str]]:
        self._load()
        return dict(self._entries)


__all__ = [
    "ChangeKeyMemo",
    "append_probe",
    "claim_change_keys",
    "claim_index_change_keys",
    "compose",
    "document_figure_change_keys",
    "head_change_keys",
    "statement_change_keys",
]
