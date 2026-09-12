"""Lazy read-only financial-note authority for company model processes.

The company-model parent, child and deterministic forecast processes all use
this factory.  It first inspects Core for an exact promoted typed admission.
When none exists it returns no projection without opening document source,
router or candidate-staging authorities.  When one exists it reconstructs the
same configured registry and replays every immutable proof before exposing a
small prompt projection or a persisted structure binding.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping

from .document_research import build_document_research_registry
from .document_research_inventory import (
    CONFIG_FILENAME, validate_inventory_config,
)
from .financial_note_evidence import (
    TARGET_REF, financial_note_evidence_binding,
    resolve_financial_note_evidence, resolve_financial_note_evidence_ref,
    validate_financial_note_target,
)
from .readonly_sqlite import connect_read_only
from .store import canonical_json, content_hash


CONTEXT_SCHEMA_VERSION = "company-model-financial-note-context-0.1"
FORECAST_BINDING_SCHEMA_VERSION = "forecast-financial-note-context-binding-0.1"


class FinancialNoteContextError(RuntimeError):
    """A configured note authority cannot be reproduced exactly."""


def _regular_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise FinancialNoteContextError(
            "document research configuration is unavailable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise FinancialNoteContextError(
                "document research configuration is not a regular file"
            )
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise FinancialNoteContextError(
                "document research configuration changed while it was read"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = _regular_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
        config = validate_inventory_config(value)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise FinancialNoteContextError(
            "document research configuration is invalid"
        ) from exc
    return config, hashlib.sha256(raw).hexdigest()


def _canonical_record(row: Any, *, label: str) -> dict[str, Any]:
    if row is None:
        raise FinancialNoteContextError(f"{label} is unavailable")
    try:
        value = json.loads(row["record_json"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise FinancialNoteContextError(f"{label} is invalid") from exc
    if (
        not isinstance(value, Mapping)
        or canonical_json(value) != row["record_json"]
        or value.get("content_hash") != row["content_hash"]
        or content_hash({key: item for key, item in value.items()
                         if key != "content_hash"}) != row["content_hash"]
    ):
        raise FinancialNoteContextError(f"{label} drifted")
    return dict(value)


def _typed_admission_refs(connection: Any, company_ref: str) -> list[str]:
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('mission_document_research_admissions',"
        "'mission_document_research_promotions')"
    )}
    if tables != {
        "mission_document_research_admissions",
        "mission_document_research_promotions",
    }:
        return []
    refs: list[str] = []
    for row in connection.execute(
        "SELECT a.record_json,a.content_hash,a.admission_id,a.company_ref "
        "FROM mission_document_research_admissions a JOIN "
        "mission_document_research_promotions p ON p.admission_ref=a.admission_id "
        "WHERE a.company_ref=? ORDER BY a.admission_id", (company_ref,),
    ):
        admission = _canonical_record(row, label="typed note admission")
        target = (
            (admission.get("planner_inquiry") or {}).get("directed_document") or {}
        ).get("evidence_target")
        if not isinstance(target, Mapping) or target.get("target_ref") != TARGET_REF:
            continue
        try:
            validate_financial_note_target(target)
        except Exception as exc:
            raise FinancialNoteContextError(
                "promoted financial note target is invalid"
            ) from exc
        if (
            admission.get("id") != row["admission_id"]
            or admission.get("company_ref") != row["company_ref"]
            or row["company_ref"] != company_ref
        ):
            raise FinancialNoteContextError(
                "promoted financial note admission columns drifted"
            )
        refs.append(admission["id"])
    if len(refs) != len(set(refs)):
        raise FinancialNoteContextError("financial note admission is duplicated")
    return refs


def _registry(*, store: Any, state_dir: Path, config: Mapping[str, Any]) -> Any:
    """Build the existing registry from readers that never provision state."""

    from .connector_authority_port import ReadOnlyConnectorReceiptReader
    from .public_web_fetch_launcher import ReadOnlyPublicWebFetchManifestReader
    from .raw_spool import RawSpoolReader

    sources = config["enabled_sources"]
    if "source:sec-edgar" not in sources:
        raise FinancialNoteContextError(
            "typed SEC note evidence is not enabled by document research policy"
        )
    # The typed target contract is SEC-only.  Do not make an unrelated missing
    # wiki, sales-note or AlphaEngine lane a prerequisite for replaying this
    # exact SEC registration.
    web = ReadOnlyPublicWebFetchManifestReader(state_dir=state_dir)
    return build_document_research_registry(
        core=store, state_dir=state_dir,
        spool=RawSpoolReader(config["spool_dir"]),
        receipt_reader=ReadOnlyConnectorReceiptReader(store.connection),
        policy=config["policy"], feed_launchers={},
        alphaengine_launcher=None, public_web_launcher=web,
        public_web_source_refs=["source:sec-edgar"],
        source_reading_limits=config["source_reading_limits"],
    )


def _prompt_record(full: Mapping[str, Any]) -> dict[str, Any]:
    binding = financial_note_evidence_binding(full)
    body = {
        "authority_ref": full["ref"],
        "authority_hash": full["content_hash"],
        "binding": binding,
        "normalized_statement": full["normalized_statement"],
        "registration_ref": full["registration_ref"],
        "registration_hash": full["registration_hash"],
        "source_authority_ref": full["source_authority_ref"],
        "source_authority_hash": full["source_authority_hash"],
        "source_content_hash": full["source_content_hash"],
        "search_proof_ref": full["search_proof_ref"],
        "search_proof_hash": full["search_proof_hash"],
        "passages": json.loads(canonical_json(full["passages"])),
    }
    return {**body, "content_hash": content_hash(body)}


def validate_financial_note_context(
    value: Mapping[str, Any], *, company_ref: str,
) -> dict[str, Any]:
    fields = {
        "schema_version", "company_ref", "config_hash", "config_file_sha256",
        "records", "content_hash",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise FinancialNoteContextError("financial note context has an invalid shape")
    if value.get("schema_version") != CONTEXT_SCHEMA_VERSION \
            or value.get("company_ref") != company_ref:
        raise FinancialNoteContextError("financial note context names another company")
    for field in ("config_hash", "config_file_sha256"):
        digest = value.get(field)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)):
            raise FinancialNoteContextError(f"financial note context {field} is invalid")
    records = value.get("records")
    if not isinstance(records, list) or not records:
        raise FinancialNoteContextError("financial note context has no authority records")
    record_fields = {
        "authority_ref", "authority_hash", "binding", "normalized_statement",
        "registration_ref", "registration_hash", "source_authority_ref",
        "source_authority_hash", "source_content_hash", "search_proof_ref",
        "search_proof_hash", "passages", "content_hash",
    }
    for item in records:
        if (
            not isinstance(item, Mapping)
            or set(item) != record_fields
            or item.get("content_hash") != content_hash({
                key: member for key, member in item.items() if key != "content_hash"
            })
            or item.get("binding", {}).get("company_ref") != company_ref
            or item.get("authority_ref") != item.get("binding", {}).get("ref")
            or item.get("authority_hash") != item.get("binding", {}).get("content_hash")
        ):
            raise FinancialNoteContextError("financial note context record drifted")
        for field in (
            "authority_hash", "registration_hash", "source_authority_hash",
            "source_content_hash", "search_proof_hash", "content_hash",
        ):
            digest = item.get(field)
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)):
                raise FinancialNoteContextError(
                    f"financial note context record {field} is invalid"
                )
    if [item["authority_ref"] for item in records] != sorted(
        item["authority_ref"] for item in records
    ) or len({item["authority_ref"] for item in records}) != len(records):
        raise FinancialNoteContextError(
            "financial note context records are not unique and sorted"
        )
    expected = content_hash({key: item for key, item in value.items()
                             if key != "content_hash"})
    if value.get("content_hash") != expected:
        raise FinancialNoteContextError("financial note context hash drifted")
    return json.loads(canonical_json(value))


def forecast_financial_note_context_binding(
    value: Mapping[str, Any], *, company_ref: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Bind the exact prompt/config authority used by a forecast parent."""

    context = validate_financial_note_context(value, company_ref=company_ref)
    if (
        not isinstance(evidence_refs, list) or not evidence_refs
        or evidence_refs != sorted(evidence_refs)
        or len(evidence_refs) != len(set(evidence_refs))
    ):
        raise FinancialNoteContextError(
            "forecast financial note refs are not unique and sorted"
        )
    known = {item["authority_ref"]: item for item in context["records"]}
    if any(ref not in known for ref in evidence_refs):
        raise FinancialNoteContextError(
            "forecast financial note authority is absent from the model context"
        )
    body = {
        "schema_version": FORECAST_BINDING_SCHEMA_VERSION,
        "company_ref": company_ref,
        "config_hash": context["config_hash"],
        "config_file_sha256": context["config_file_sha256"],
        "context_hash": context["content_hash"],
        "authorities": [{
            "ref": ref,
            "authority_hash": known[ref]["authority_hash"],
            "prompt_record_hash": known[ref]["content_hash"],
        } for ref in evidence_refs],
    }
    return {**body, "content_hash": content_hash(body)}


class FinancialNoteReadContext(AbstractContextManager):
    """Lazy process-local owner of the read-only supporting connections."""

    def __init__(self, *, store: Any, state_dir: str | Path) -> None:
        if not callable(getattr(getattr(store, "connection", None), "execute", None)):
            raise TypeError("financial note context requires an open Core store")
        self.store = store
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.config_path = self.state_dir / CONFIG_FILENAME
        self._config: dict[str, Any] | None = None
        self._config_file_sha256: str | None = None
        self._registry: Any | None = None
        self._router: sqlite3.Connection | None = None
        self._staging: sqlite3.Connection | None = None

    def _open(self) -> None:
        config, file_hash = _read_config(self.config_path)
        if self._config is not None:
            if (content_hash(config) != content_hash(self._config)
                    or file_hash != self._config_file_sha256):
                raise FinancialNoteContextError(
                    "document research configuration changed during company modelling"
                )
            return
        registry = _registry(store=self.store, state_dir=self.state_dir, config=config)
        router = connect_read_only(self.state_dir / "model-router.sqlite")
        try:
            staging = connect_read_only(
                self.state_dir / "research-review" / "candidate-staging.sqlite"
            )
        except BaseException:
            router.close()
            raise
        router.row_factory = sqlite3.Row
        staging.row_factory = sqlite3.Row
        self._config = config
        self._config_file_sha256 = file_hash
        self._registry = registry
        self._router = router
        self._staging = staging

    def _resolve_admission(self, admission_ref: str) -> dict[str, Any]:
        self._open()
        assert self._registry is not None and self._router is not None \
            and self._staging is not None
        return resolve_financial_note_evidence(
            core_connection=self.store.connection,
            router_connection=self._router,
            staging_connection=self._staging,
            registry=self._registry,
            admission_ref=admission_ref,
        )

    def projection(self, company_ref: str) -> dict[str, Any] | None:
        refs = _typed_admission_refs(self.store.connection, company_ref)
        if not refs:
            return None
        records = sorted((_prompt_record(self._resolve_admission(ref)) for ref in refs),
                         key=lambda item: item["authority_ref"])
        assert self._config is not None and self._config_file_sha256 is not None
        body = {
            "schema_version": CONTEXT_SCHEMA_VERSION,
            "company_ref": company_ref,
            "config_hash": content_hash(self._config),
            "config_file_sha256": self._config_file_sha256,
            "records": records,
        }
        return validate_financial_note_context(
            {**body, "content_hash": content_hash(body)}, company_ref=company_ref,
        )

    def resolver(
        self, evidence_ref: str, *, expected_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._open()
        if expected_context is not None:
            expected = validate_financial_note_context(
                expected_context, company_ref=str(expected_context.get("company_ref") or ""),
            )
            assert self._config is not None and self._config_file_sha256 is not None
            if (expected["config_hash"] != content_hash(self._config)
                    or expected["config_file_sha256"] != self._config_file_sha256):
                raise FinancialNoteContextError(
                    "document research configuration differs from company model state"
                )
        assert self._registry is not None and self._router is not None \
            and self._staging is not None
        full = resolve_financial_note_evidence_ref(
            core_connection=self.store.connection,
            router_connection=self._router,
            staging_connection=self._staging,
            registry=self._registry,
            evidence_ref=evidence_ref,
        )
        binding = financial_note_evidence_binding(full)
        if expected_context is not None:
            held = {item["authority_ref"]: item for item in expected["records"]}
            record = held.get(evidence_ref)
            if record is None or record["authority_hash"] != full["content_hash"] \
                    or record["binding"] != binding:
                raise FinancialNoteContextError(
                    "financial note authority differs from company model state"
                )
        return binding

    def close(self) -> None:
        for connection in (self._staging, self._router):
            if connection is not None:
                connection.close()
        self._staging = None
        self._router = None
        self._registry = None
        self._config = None
        self._config_file_sha256 = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = [
    "CONTEXT_SCHEMA_VERSION", "FORECAST_BINDING_SCHEMA_VERSION",
    "FinancialNoteContextError", "FinancialNoteReadContext",
    "forecast_financial_note_context_binding", "validate_financial_note_context",
]
