from __future__ import annotations

import json
import unittest

from scripts.successor_writer_token_transition import (
    WriterTokenTransitionError, bootstrap_serialized_after,
    validate_rollback_state,
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
