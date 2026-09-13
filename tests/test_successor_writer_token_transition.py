from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.successor_writer_token_transition import (
    WriterTokenTransitionError, bootstrap_serialized_after, build_transition,
    source_core_operations, validate_rollback_state, validate_transition,
)


def wire(operations=("a", "b"), token="secret") -> bytes:
    return (json.dumps({"schema_version": "0.1", "principals": [{
        "principal_id": "core", "token": token,
        "operations": list(operations), "allowed_invocation_refs": [],
        "work_order_refs": [], "unrestricted": True, "actor_ref": None,
    }, {
        "principal_id": "owner", "token": "owner-secret",
        "operations": ["approve"], "allowed_invocation_refs": [],
        "work_order_refs": [], "unrestricted": False, "actor_ref": "owner:x",
    }]}, sort_keys=True, separators=(",", ":")) + "\n").encode()


class WriterTokenTransitionTests(unittest.TestCase):
    def test_exact_bootstrap_append_preserves_every_other_field(self):
        before = wire()
        after = bootstrap_serialized_after(before, ["a", "b"], ["a", "b", "settle"])
        b, a = json.loads(before), json.loads(after)
        self.assertEqual(["a", "b", "settle"], a["principals"][0]["operations"])
        a["principals"][0]["operations"] = b["principals"][0]["operations"]
        self.assertEqual(b, a)

    def test_foreign_baseline_operation_refuses_projection(self):
        with self.assertRaisesRegex(WriterTokenTransitionError, "predecessor"):
            bootstrap_serialized_after(wire(("a", "foreign")), ["a", "b"],
                                       ["a", "b", "settle"])

    def test_projection_rejects_authority_and_serializer_ambiguity(self):
        cases = []
        for mutate in (
            lambda value: value.update(schema_version="foreign"),
            lambda value: value.update(extra="field"),
            lambda value: value["principals"][0].update(unrestricted=False),
            lambda value: value["principals"][1].update(unrestricted=True),
            lambda value: value["principals"][1].update(principal_id="core"),
            lambda value: value["principals"][1]["operations"].append("approve"),
            lambda value: value["principals"][1].update(token=""),
        ):
            value = json.loads(wire())
            mutate(value)
            cases.append(json.dumps(value).encode())
        cases.append(b"[]")
        cases.append(b"not-json")
        for before in cases:
            with self.subTest(before=before):
                with self.assertRaises(WriterTokenTransitionError):
                    bootstrap_serialized_after(before, ["a", "b"], ["a", "b", "settle"])

    def test_projection_rejects_ambiguous_source_operation_lists(self):
        for old, new in ((["b", "a"], ["a", "b", "settle"]),
                         (["a", "a"], ["a", "settle"]),
                         (["a"], ["a", "a", "settle"])):
            with self.subTest(old=old, new=new):
                with self.assertRaisesRegex(WriterTokenTransitionError, "source operations"):
                    bootstrap_serialized_after(wire(), old, new)

    def test_build_rejects_symlinked_token_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual.json"
            actual.write_bytes(wire())
            link = root / "writer-tokens.json"
            link.symlink_to(actual)
            with self.assertRaisesRegex(WriterTokenTransitionError, "baseline"):
                build_transition(before_path=link, predecessor_root=root,
                                 predecessor_commit="a" * 40, successor_root=root,
                                 successor_commit="b" * 40)

    def test_source_rejects_symlink_root_and_noncanonical_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "checkout"
            link.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(WriterTokenTransitionError, "root"):
                source_core_operations(link, "a" * 40)
            with self.assertRaisesRegex(WriterTokenTransitionError, "commit"):
                source_core_operations(root, "HEAD")

    @patch("scripts.successor_writer_token_transition.source_core_operations")
    def test_validation_rejects_malformed_nested_proof_closed(self, operations):
        row = {"kind": "bootstrap_source_operations_append",
               "target": "writer-tokens.json", "principal_id": "core",
               "before_sha256": "x", "predicted_after_sha256": "y",
               "predecessor": [], "successor": {}, "added_operations": []}
        with self.assertRaisesRegex(WriterTokenTransitionError, "proof"):
            validate_transition(row, before_bytes=wire(), predecessor_root=Path("old"),
                                successor_root=Path("new"))
        operations.assert_not_called()

    def test_rollback_accepts_only_exact_endpoints_without_rewriting(self):
        before = wire(); after = bootstrap_serialized_after(before, ["a", "b"],
                                                            ["a", "b", "settle"])
        validate_rollback_state(before, after, before)
        validate_rollback_state(before, after, after)
        for changed in (wire(token="rotated"),
                        bootstrap_serialized_after(before, ["a", "b"],
                                                   ["a", "b", "extra", "settle"])):
            with self.assertRaisesRegex(WriterTokenTransitionError, "changed"):
                validate_rollback_state(before, after, changed)


if __name__ == "__main__":
    unittest.main()
