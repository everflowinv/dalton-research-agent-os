"""Every option the CLI accepts must reach every path it can dispatch to.

--max-daily-cost-usd was threaded into the rehearse path and not into the live
one, so the rehearsal passed and the owner's actual command died with
"live() got an unexpected keyword argument". A rehearsal that does not
exercise the same signature as the real run is not a rehearsal.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_extraction_authority_chain.py"
ENTRY_POINTS = ("rehearse", "live", "build_chain")


class ChainOptionTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    def parser_options(self) -> set[str]:
        """The dest names argparse would produce for every --option."""

        names: set[str] = set()
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    names.add(str(arg.value)[2:].replace("-", "_"))
        return names

    def accepted_by(self, function: str) -> set[str]:
        node = next(n for n in self.tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == function)
        return {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}

    def test_the_budget_options_reach_both_paths(self):
        budget = {"max_daily_paid_calls", "max_alphaengine_calls_24h", "max_daily_cost_usd"}
        self.assertTrue(budget <= self.parser_options(), "an option was removed from the CLI")
        for entry in ENTRY_POINTS:
            self.assertTrue(
                budget <= self.accepted_by(entry),
                f"{entry} does not accept {sorted(budget - self.accepted_by(entry))}",
            )

    def test_rehearse_and_live_accept_the_same_options(self):
        # The whole point of a rehearsal is that it is the same call.
        self.assertEqual(
            self.accepted_by("rehearse") - {"target"},
            self.accepted_by("live"),
        )

    def test_writer_rejects_a_mission_budget_outside_bound_authorities(self):
        from dalton_core.writer_server import WriterServer, ValidationError
        server = object.__new__(WriterServer)
        old = {"max_daily_paid_calls": 1, "max_daily_cost_usd": 1,
               "max_alphaengine_calls_24h": 1}
        server._coverage_mission = SimpleNamespace(mission=lambda ref: {"budget": old})
        server._agenda = SimpleNamespace(mandate_version=lambda ref: {
            "constraints": {"research_budget": old}})
        server._store = SimpleNamespace(active_policy_version=lambda: SimpleNamespace(
            to_dict=lambda: {"policy": {"research_budget": old}}))
        params = {"mission_ref": "coverage-mission:test", "prior_version_ref": "v1",
                  "budget": {**old, "max_daily_cost_usd": 2},
                  "bindings": {"mandate_version": {"ref": "m1"}}}
        with self.assertRaisesRegex(ValidationError, "policy/mandate/constitution cascade"):
            server._op_create_coverage_mission(params)


if __name__ == "__main__":
    unittest.main()
