import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.connector import ConnectorStore, source_envelope_content_hash
from dalton_core.document_index import (
    DocumentIndex,
    DocumentIndexConflict,
    DocumentIndexValidationError,
    make_document_index_input,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.raw_spool import RawSpool
from dalton_core.store import DaltonStore, content_hash


WHEN = "2026-01-01T00:00:00.000000+00:00"


def _model_invocation(identifier: str, output_refs: list[str]) -> dict:
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": WHEN,
        "work_order_ref": f"work:{identifier}",
        "profile_ref": "profile:connector",
        "granularity": "task",
        "capability": "connector",
        "provider": "fixture",
        "model": "fixture",
        "model_family": "fixture-family",
        "input_refs": [],
        "output_refs": output_refs,
        "started_at": WHEN,
        "completed_at": WHEN,
        "usage": {},
        "side_effects": [],
        "runtime_ref": "runtime:fixture",
        "actor_ref": "runner:fixture",
        "parent_ref": None,
        "environment_hash": "e" * 64,
    }


def _connector_execution(identifier: str, output_refs: list[str]) -> dict:
    return {
        "schema_version": "0.1",
        "id": identifier,
        "created_at": WHEN,
        "kind": "connector",
        "work_order_ref": "work:sec",
        "profile_ref": "profile:sec",
        "capability": "capability:sec",
        "input_refs": ["call:sec:list-filings"],
        "output_refs": output_refs,
        "started_at": WHEN,
        "completed_at": None,
        "side_effects": [],
        "runtime_ref": "runtime:sec",
        "actor_ref": "runner:sec",
        "parent_ref": None,
        "environment_hash": "4" * 64,
    }


class DocumentIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DaltonStore(":memory:")
        self.obs = ObservabilityStore(self.store)
        self.connector = ConnectorStore(self.store)
        self.raw_spool = RawSpool(Path(self.temp.name), max_total_bytes=2_000_000)
        self.raws: dict[str, bytes] = {}
        self.artifacts: dict[str, dict] = {}
        self._add_artifact(
            "artifact:plain",
            "artifact-version:plain",
            "AI 半导体报告",
            "这是半导体行业的研究摘要，包含存储与设备。",
            access_class="public",
        )
        self._add_artifact(
            "artifact:internal",
            "artifact-version:internal",
            "内部研究材料",
            "内部材料不可默认返回。",
            access_class="internal",
        )
        self._add_artifact(
            "artifact:json",
            "artifact-version:json",
            "SEC submissions metadata",
            json.dumps({"issuer": "0000789019", "filing": "半导体 metadata"}, ensure_ascii=False).encode(),
            access_class="public",
            media_type="application/json",
            execution_kind="connector",
        )
        self._add_sec_source("artifact:json", "artifact-version:json")
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)

    def _add_artifact(
        self,
        artifact_ref: str,
        version_id: str,
        title: str,
        raw: str | bytes,
        *,
        access_class: str,
        media_type: str = "text/plain",
        execution_kind: str = "model",
    ):
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.raws[version_id] = raw
        execution_id = f"execution:{version_id}"
        if execution_kind == "connector":
            with self.store._transaction() as cur:
                self.store._ensure_execution_invocation(
                    cur, _connector_execution(execution_id, [artifact_ref])
                )
        else:
            self.store.register_invocation(_model_invocation(execution_id, [artifact_ref]))
        digest = hashlib.sha256(raw).hexdigest()
        sink = self.raw_spool.open_sink(
            f"raw-sink:{hashlib.sha256(version_id.encode()).hexdigest()}",
            max_response_bytes=max(1, len(raw) + 1),
        )
        sink.write(raw)
        sink.finalize()
        self.artifacts[version_id] = self.obs.register_artifact_version_v2(
            artifact_ref,
            version_id=version_id,
            title=title,
            kind="document",
            media_type=media_type,
            artifact_content_hash=digest,
            size_bytes=len(raw),
            storage_locator=f"spool:objects/{digest[:2]}/{digest}",
            producer_execution_ref=execution_id,
            result_envelope_ref=f"result:{version_id}",
            result_envelope_hash="a" * 64,
            access_class=access_class,
            preview_status="available",
            actor_ref="system:artifact",
        )

    def _add_sec_source(self, artifact_ref: str, version_id: str):
        artifact = self.artifacts[version_id]
        source_identity = {
            "source_ref": "source:sec-edgar",
            "source_type": "official_filing",
            "source_version": "0.1",
        }
        profile = {
            "schema_version": "0.1",
            "id": "profile:sec",
            "created_at": WHEN,
            "connector_ref": "connector:sec",
            "version": 1,
            "prior_version_ref": None,
            "capability_id": "capability:sec",
            "descriptor_revision_ref": "descriptor:sec:v1",
            "descriptor_hash": "1" * 64,
            "source_identity": source_identity,
            "source_hash": content_hash(source_identity),
            "schema_hash": "2" * 64,
            "catalog_epoch": 1,
            "adapter_ref": "adapter:sec",
            "adapter_hash": "3" * 64,
            "runner_runtime_ref": "runtime:sec",
            "runner_actor_ref": "runner:sec",
            "runner_environment_hash": "4" * 64,
            "allowed_operations": ["list_filings"],
            "allowed_hosts": ["data.sec.gov"],
            "auth_mode": "none",
            "credential_slot_refs": [],
            "input_schema_refs": {"list_filings": "schema:sec:in"},
            "input_schema_hashes": {"list_filings": "5" * 64},
            "output_schema_refs": {"list_filings": "schema:sec:out"},
            "output_schema_hashes": {"list_filings": "6" * 64},
            "pagination": {"mode": "none", "cursor_field": None, "max_pages": 1},
            "completeness": {"list_filings": "enumerated"},
            "max_response_bytes": 1_000_000,
            "max_records": 100,
            "timeout_ms": 1000,
            "access_policy_ref": "policy:access",
            "retention_policy_ref": "policy:retention",
            "terms_policy_ref": "policy:terms",
            "network_policy": {
                "allowed_schemes": ["https"],
                "allow_redirects": False,
                "max_redirects": 0,
                "resolve_public_only": True,
            },
        }
        profile["content_hash"] = content_hash(profile)
        params = {"issuer": "789019", "date_from": "2025-01-01", "date_to": "2025-12-31"}
        call = {
            "schema_version": "0.1",
            "id": "call:sec:list-filings",
            "created_at": WHEN,
            "work_order_ref": "work:sec",
            "work_order_hash": "7" * 64,
            "connector_profile_ref": profile["id"],
            "operation": "list_filings",
            "parameters": params,
            "query_hash": content_hash({"operation": "list_filings", "parameters": params}),
        }
        call["content_hash"] = content_hash(call)
        invocation = {
            "schema_version": "0.1",
            "id": "connector-invocation:sec",
            "created_at": WHEN,
            "work_order_ref": "work:sec",
            "work_order_hash": "7" * 64,
            "connector_profile_ref": profile["id"],
            "connector_profile_hash": profile["content_hash"],
            "call_spec_ref": call["id"],
            "call_spec_hash": call["content_hash"],
            "capability_lease_ref": "lease:sec",
            "capability_lease_hash": "8" * 64,
            "descriptor_revision_ref": profile["descriptor_revision_ref"],
            "catalog_epoch": 1,
            "logical_invocation_key": "logical:sec",
        }
        execution_ref = f"execution:{version_id}"
        execution_hash = self.store.connection.execute(
            "SELECT content_hash FROM execution_invocations WHERE execution_id=?",
            (execution_ref,),
        ).fetchone()[0]
        invocation["content_hash"] = content_hash(invocation)
        source = {
            "schema_version": "0.1",
            "id": "source-envelope:sec",
            "created_at": WHEN,
            "connector_invocation_ref": invocation["id"],
            "connector_profile_ref": profile["id"],
            "physical_attempt_refs": ["attempt:sec"],
            "result_physical_attempt_ref": "attempt:sec",
            "source": source_identity["source_ref"],
            "operation": "list_filings",
            "source_record_refs": ["sec:filing:1"],
            "published_at": "2025-06-01T00:00:00.000000+00:00",
            "updated_at": None,
            "as_of": None,
            "retrieved_at": "2026-01-01T00:00:00.000000+00:00",
            "cursor": None,
            "provider_request_id": "req:sec",
            "raw_artifact_version_ref": artifact["id"],
            "raw_response_hash": artifact["artifact_content_hash"],
            "source_schema_hash": "6" * 64,
            "source_content_hash": None,
            "completeness": "enumerated",
            "status": "complete",
            "access_policy_ref": profile["access_policy_ref"],
            "retention_policy_ref": profile["retention_policy_ref"],
            "terms_policy_ref": profile["terms_policy_ref"],
            "error": None,
        }
        source["source_content_hash"] = source_envelope_content_hash(source)
        source["content_hash"] = content_hash(source)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO connector_profile_versions"
                "(profile_version_id,connector_ref,version_number,prior_version_ref,capability_id,"
                "descriptor_revision_ref,descriptor_hash,source_hash,schema_hash,catalog_epoch,adapter_ref,"
                "adapter_hash,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (profile["id"], profile["connector_ref"], 1, None, profile["capability_id"], profile["descriptor_revision_ref"], profile["descriptor_hash"], profile["source_hash"], profile["schema_hash"], 1, profile["adapter_ref"], profile["adapter_hash"], json.dumps(profile), profile["content_hash"], profile["created_at"]),
            )
            cur.execute(
                "INSERT INTO connector_call_specs(call_spec_id,work_order_ref,work_order_hash,connector_profile_ref,operation,query_hash,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (call["id"], call["work_order_ref"], call["work_order_hash"], call["connector_profile_ref"], call["operation"], call["query_hash"], json.dumps(call), call["content_hash"], call["created_at"]),
            )
            cur.execute(
                "INSERT INTO connector_invocations(connector_invocation_id,execution_ref,execution_hash,work_order_ref,work_order_hash,connector_profile_ref,connector_profile_hash,call_spec_ref,call_spec_hash,capability_lease_ref,capability_lease_hash,descriptor_revision_ref,catalog_epoch,logical_invocation_key,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (invocation["id"], execution_ref, execution_hash, invocation["work_order_ref"], invocation["work_order_hash"], invocation["connector_profile_ref"], invocation["connector_profile_hash"], invocation["call_spec_ref"], invocation["call_spec_hash"], invocation["capability_lease_ref"], invocation["capability_lease_hash"], invocation["descriptor_revision_ref"], invocation["catalog_epoch"], invocation["logical_invocation_key"], json.dumps(invocation), invocation["content_hash"], invocation["created_at"]),
            )
            cur.execute(
                "INSERT INTO connector_source_envelopes(source_envelope_id,connector_invocation_ref,connector_profile_ref,raw_artifact_version_ref,raw_response_hash,completeness,status,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (source["id"], source["connector_invocation_ref"], source["connector_profile_ref"], source["raw_artifact_version_ref"], source["raw_response_hash"], source["completeness"], source["status"], json.dumps(source), source["content_hash"], source["created_at"]),
            )

    def _index(self, *, visible=("public",)) -> DocumentIndex:
        index = DocumentIndex(
            ":memory:",
            observability=self.obs,
            connector_store=self.connector,
            raw_spool=self.raw_spool,
            visible_access_classes=visible,
        )
        self.addCleanup(index.close)
        return index

    def test_exact_raw_extraction_and_facets(self):
        idx = self._index()
        inputs = [
            make_document_index_input(
                self.artifacts["artifact-version:plain"]["id"],
                self.artifacts["artifact-version:plain"]["content_hash"],
                "utf8",
                created_at=WHEN,
            ),
            make_document_index_input(
                self.artifacts["artifact-version:json"]["id"],
                self.artifacts["artifact-version:json"]["content_hash"],
                "json",
                created_at=WHEN,
            ),
        ]
        first = idx.rebuild(inputs, created_at=WHEN)
        self.assertEqual(idx.count(), 2)
        result = idx.search(
            "半导体",
            company_ref="company:sec-cik:0000789019",
            source_type="official_filing",
            content_type="document",
            media_type="application/json",
            date_from="2025-01-01",
            date_to="2025-12-31",
        )
        self.assertEqual([row["artifact_version_ref"] for row in result], ["artifact-version:json"])
        self.assertEqual(first, idx.snapshot())

    def test_caller_text_metadata_and_hash_rebinding_fail_closed(self):
        idx = self._index()
        good = make_document_index_input(
            self.artifacts["artifact-version:plain"]["id"],
            self.artifacts["artifact-version:plain"]["content_hash"],
            "utf8",
            created_at=WHEN,
        )
        forged = dict(good)
        forged["company_ref"] = "company:forged"
        with self.assertRaises(DocumentIndexValidationError):
            idx.rebuild([forged], created_at=WHEN)
        forged = dict(good)
        forged["artifact_version_hash"] = "f" * 64
        forged["id"] = (
            f"document-input:{forged['artifact_version_ref']}:{forged['artifact_version_hash']}:utf8"
        )
        forged["content_hash"] = content_hash(
            {key: value for key, value in forged.items() if key != "content_hash"}
        )
        with self.assertRaises(DocumentIndexConflict):
            idx.rebuild([forged], created_at=WHEN)

    def test_artifact_sql_hash_binding_fails_closed(self):
        idx = self._index()
        artifact = self.artifacts["artifact-version:plain"]
        item = make_document_index_input(artifact["id"], artifact["content_hash"], "utf8", created_at=WHEN)
        self.store.connection.execute("BEGIN")
        try:
            self.store.connection.execute("DROP TRIGGER observability_artifact_versions_v2_no_update")
            self.store.connection.execute(
                "UPDATE observability_artifact_versions_v2 SET content_hash=? WHERE version_id=?",
                ("0" * 64, artifact["id"]),
            )
            with self.assertRaises(DocumentIndexConflict):
                idx.rebuild([item], created_at=WHEN)
        finally:
            self.store.connection.execute("ROLLBACK")

    def test_no_caller_supplied_text_and_binary_requires_supported_extractor(self):
        idx = self._index()
        raw = self.raws["artifact-version:plain"]
        self.assertNotIn(raw.decode(), make_document_index_input(self.artifacts["artifact-version:plain"]["id"], self.artifacts["artifact-version:plain"]["content_hash"], "utf8", created_at=WHEN))
        bad = make_document_index_input(self.artifacts["artifact-version:plain"]["id"], self.artifacts["artifact-version:plain"]["content_hash"], "json", created_at=WHEN)
        with self.assertRaises(DocumentIndexValidationError):
            idx.rebuild([bad], created_at=WHEN)

    def test_unknown_source_has_no_company_facet(self):
        idx = self._index()
        item = make_document_index_input(
            self.artifacts["artifact-version:plain"]["id"],
            self.artifacts["artifact-version:plain"]["content_hash"],
            "utf8",
            created_at=WHEN,
        )
        snapshot = idx.rebuild([item], created_at=WHEN)
        document = snapshot["documents"][0]
        self.assertIsNone(document["source_type"])
        self.assertEqual(document["company_refs"], [])
        self.assertIsNone(document["company_parser_ref"])

    def test_raw_object_hash_and_size_are_rechecked(self):
        idx = self._index()
        artifact = self.artifacts["artifact-version:plain"]
        item = make_document_index_input(artifact["id"], artifact["content_hash"], "utf8", created_at=WHEN)
        object_path = (
            Path(self.temp.name)
            / "connector-spool"
            / "objects"
            / artifact["artifact_content_hash"][:2]
            / artifact["artifact_content_hash"]
        )
        object_path.write_bytes(b"tampered raw bytes")
        with self.assertRaises(DocumentIndexConflict):
            idx.rebuild([item], created_at=WHEN)

    def test_projection_file_is_owner_only(self):
        path = Path(self.temp.name) / "document-index.sqlite"
        idx = DocumentIndex(
            path,
            observability=self.obs,
            connector_store=self.connector,
            raw_spool=self.raw_spool,
        )
        self.addCleanup(idx.close)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_clear_and_deterministic_rebuild(self):
        idx = self._index()
        item = make_document_index_input(self.artifacts["artifact-version:plain"]["id"], self.artifacts["artifact-version:plain"]["content_hash"], "utf8", created_at=WHEN)
        with self.assertRaises(DocumentIndexConflict):
            idx.rebuild([item, item], created_at=WHEN)
        first = idx.rebuild([item], created_at=WHEN)
        authority_count = self.store.connection.execute("SELECT COUNT(*) FROM observability_artifact_versions_v2").fetchone()[0]
        idx.clear()
        self.assertEqual(idx.count(), 0)
        second = idx.rebuild([item], created_at=WHEN)
        self.assertEqual(first, second)
        self.assertEqual(authority_count, self.store.connection.execute("SELECT COUNT(*) FROM observability_artifact_versions_v2").fetchone()[0])

    def test_rebuild_rolls_back_when_integrity_gate_fails(self):
        idx = self._index(visible=("public", "internal"))
        old_artifact = self.artifacts["artifact-version:plain"]
        old_item = make_document_index_input(
            old_artifact["id"], old_artifact["content_hash"], "utf8", created_at=WHEN
        )
        new_artifact = self.artifacts["artifact-version:internal"]
        new_item = make_document_index_input(
            new_artifact["id"], new_artifact["content_hash"], "utf8", created_at=WHEN
        )
        old_snapshot = idx.rebuild([old_item], created_at=WHEN)
        with patch.object(
            idx,
            "_assert_projection_integrity",
            side_effect=DocumentIndexConflict("injected projection fault"),
        ):
            with self.assertRaises(DocumentIndexConflict):
                idx.rebuild([new_item], created_at=WHEN)
        self.assertEqual(idx.count(), 1)
        self.assertEqual(idx.snapshot(), old_snapshot)
        self.assertEqual(len(idx.search("半导体")), 1)
        idx.rebuild([new_item], created_at=WHEN)
        self.assertEqual(idx.count(), 1)
        self.assertEqual(len(idx.search("内部材")), 1)

    def test_visibility_and_fts_boundary(self):
        idx = self._index()
        inputs = [make_document_index_input(row["id"], row["content_hash"], "utf8", created_at=WHEN) for row in self.artifacts.values() if row["media_type"] == "text/plain"]
        idx.rebuild(inputs, created_at=WHEN)
        self.assertEqual(idx.search("内部"), [])
        with self.assertRaises(DocumentIndexValidationError):
            idx.search("\x00")
        privileged = self._index(visible=("public", "internal"))
        privileged.rebuild(inputs, created_at=WHEN)
        self.assertEqual(len(privileged.search("内部材")), 1)

    def test_projection_filter_tamper_and_fts_delete_all_fail_closed(self):
        artifact = self.artifacts["artifact-version:internal"]
        item = make_document_index_input(artifact["id"], artifact["content_hash"], "utf8", created_at=WHEN)

        idx = self._index(visible=("public", "internal"))
        idx.rebuild([item], created_at=WHEN)
        idx.connection.execute(
            "UPDATE document_index_documents SET access_class='public' WHERE rowid=1"
        )
        # A SQL filter over the mutable projection column is not enough: the
        # record and every filter-relevant column must still agree.
        with self.assertRaises(DocumentIndexConflict):
            idx.search("半导体")

        idx = self._index()
        idx.rebuild([item], created_at=WHEN)
        # External-content FTS joins can still see the content row after this
        # operation.  The rank=1 integrity-check must detect the missing
        # inverted-index entries before a query is returned.
        idx.connection.execute(
            "INSERT INTO document_index_fts(document_index_fts) VALUES('delete-all')"
        )
        with self.assertRaises(DocumentIndexConflict):
            idx.search("半导体")

    def test_fts_main_table_body_tamper_fails_checksum(self):
        idx = self._index()
        artifact = self.artifacts["artifact-version:plain"]
        item = make_document_index_input(artifact["id"], artifact["content_hash"], "utf8", created_at=WHEN)
        idx.rebuild([item], created_at=WHEN)
        idx.connection.execute(
            "UPDATE document_index_documents SET extracted_text='伪造正文' WHERE rowid=1"
        )
        with self.assertRaises(DocumentIndexConflict):
            idx.search("半导体")

    def test_source_profile_and_call_hash_links_fail_closed(self):
        artifact = self.artifacts["artifact-version:json"]
        item = make_document_index_input(artifact["id"], artifact["content_hash"], "json", created_at=WHEN)
        cases = (
            ("connector_source_envelopes_no_update", "connector_source_envelopes", "source_envelope_id", "source-envelope:sec"),
            ("connector_profile_versions_no_update", "connector_profile_versions", "profile_version_id", "profile:sec"),
            ("connector_call_specs_no_update", "connector_call_specs", "call_spec_id", "call:sec:list-filings"),
        )
        for trigger, table, id_column, identifier in cases:
            with self.subTest(table=table):
                idx = self._index()
                self.store.connection.execute("BEGIN")
                try:
                    # DDL is transactional in SQLite.  Roll it back after
                    # each subcase so a prior corruption cannot mask the
                    # next exact authority link.
                    self.store.connection.execute(f"DROP TRIGGER {trigger}")
                    self.store.connection.execute(
                        f"UPDATE {table} SET content_hash=? WHERE {id_column}=?",
                        ("0" * 64, identifier),
                    )
                    with self.assertRaises(DocumentIndexConflict):
                        idx.rebuild([item], created_at=WHEN)
                finally:
                    self.store.connection.execute("ROLLBACK")

    def test_empty_projection_and_cjk_boundary(self):
        idx = self._index()
        snap = idx.rebuild([], created_at=WHEN)
        self.assertEqual(idx.count(), 0)
        self.assertEqual(idx.search("半导体"), [])
        self.assertTrue(snap["authority_snapshot_ref"].startswith("document-authority-snapshot:"))
        # trigram supports the three-codepoint CJK term above; two-codepoint
        # substring recall is intentionally not promised by this contract.
        self.assertEqual(idx.search("存储"), [])

    def test_query_operators_are_literal_and_paging_dates_are_bounded(self):
        idx = self._index()
        inputs = [
            make_document_index_input(row["id"], row["content_hash"], "utf8", created_at=WHEN)
            for row in self.artifacts.values()
            if row["media_type"] == "text/plain"
        ]
        idx.rebuild(inputs, created_at=WHEN)
        self.assertEqual(idx.search('" OR *'), [])
        self.assertLessEqual(len(idx.search("半导体", limit=1)), 1)
        self.assertEqual(idx.search("半导体", date_from="2026-08-16"), [])
        with self.assertRaises(DocumentIndexValidationError):
            idx.search("半导体", date_from="2026-01-02", date_to="2026-01-01")
        for limit in (0, 1001):
            with self.subTest(limit=limit), self.assertRaises(DocumentIndexValidationError):
                idx.search("半导体", limit=limit)


if __name__ == "__main__":
    unittest.main()
