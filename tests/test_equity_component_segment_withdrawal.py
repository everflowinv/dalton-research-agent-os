"""Synthetic equity/non-segment selections and the retained generic replay."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "fixtures" / "equity-component-withdrawal" / "withdraw.sh"
REFS = ROOT / "tests" / "fixtures" / "equity-component-withdrawal" / "refs.tsv"
REPLAY = ROOT / "scripts" / "replay_equity_component_segment_claims_2026_09_30.py"
REF_RE = re.compile(r"^claim-version:[0-9a-f]{64}$")


def _load_replay():
    spec = importlib.util.spec_from_file_location("replay_equity_segment", REPLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refs() -> dict[tuple[str, str], list[str]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for raw in REFS.read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        group, env, ref, _desc = raw.split("\t", 3)
        groups.setdefault((group, env), []).append(ref)
    return groups


class WithdrawalScriptTests(unittest.TestCase):
    def test_the_script_is_dry_run_by_default_and_runs_from_a_release(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        if shutil.which("zsh"):
            subprocess.run(["zsh", "-n", str(SCRIPT)], check=True)
        self.assertIn("MODE=${1:-dry-run}", text)
        self.assertIn("ACTOR=${ACTOR:-human:owner}", text)
        self.assertIn("release_path", text)
        self.assertNotIn(".venv/bin/python", text)
        self.assertIn("dalton_core.claim_admission_cli retire", text)
        self.assertIn("DALTON_WORKSPACE_MANIFEST=$WS_MANIFEST", text)
        self.assertEqual(text.count("--apply --actor"), 1)
        self.assertEqual(text.count('--expect-selection "$SEL"'), 2)
        # The optional group is not part of the default run.
        default = text.split('elif [[ $ONLY == C ]]', 1)[0]
        self.assertNotIn("refs_of renamed", default)

    def test_each_group_is_held_to_its_reviewed_hash(self) -> None:
        from dalton_core.claim_admission_cli import selection_hash

        text = SCRIPT.read_text(encoding="utf-8")
        groups = _refs()
        every = [ref for refs in groups.values() for ref in refs]
        self.assertTrue(all(REF_RE.match(ref) for ref in every))
        self.assertEqual(len(every), len(set(every)), "a claim is listed twice")
        pinned = {
            ("equity", "alpha"): ("SEL_A_ALPHA", 2),
            ("equity", "beta"): ("SEL_A_BETA", 2),
            ("other", "alpha"): ("SEL_B_ALPHA", 2),
            ("other", "beta"): ("SEL_B_BETA", 2),
            ("renamed", "alpha"): ("SEL_C_ALPHA", 2),
            ("renamed", "beta"): ("SEL_C_BETA", 2),
        }
        for key, (variable, count) in pinned.items():
            refs = groups.get(key, [])
            self.assertEqual(len(refs), count, key)
            expected = re.search(variable + r"=\$\{" + variable + r":-([0-9a-f]{64})\}",
                                 text).group(1)
            self.assertEqual(expected, selection_hash(refs), key)
            group, env = key
            self.assertRegex(text, r'"\$' + variable + r'" \$\{\(f\)"\$\(refs_of '
                             + group + " " + env + r'\)"\}')
        self.assertEqual(set(groups), set(pinned))


class ReplayTests(unittest.TestCase):
    """The replay selects exactly what the promoter now refuses."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        connection = sqlite3.connect(self.state / "core.sqlite")
        connection.executescript("""
            CREATE TABLE quantitative_claim_promotions(promotion_id TEXT PRIMARY KEY,
                company_ref TEXT, metric_or_aspect TEXT, period TEXT, origin_kind TEXT,
                origin_ref TEXT, value TEXT, unit TEXT, disposition TEXT, claim_version_ref TEXT);
            CREATE TABLE coverage_mission_statement_lines(line_id TEXT PRIMARY KEY,
                ingest_id TEXT, concept TEXT, label TEXT, dimension_axis TEXT,
                dimension_member TEXT, dimension_count INTEGER);
            CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT PRIMARY KEY,
                entity_name TEXT, accession TEXT, form TEXT);
            CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, prior_version_id TEXT);
            CREATE TABLE claim_retirement_decisions(claim_version_ref TEXT, decision TEXT);
            CREATE TABLE claim_retirement_reinstatements(claim_version_ref TEXT);
        """)
        connection.execute("INSERT INTO coverage_mission_statement_filings VALUES"
                           "('i1','Northvale Research Fixture','synthetic-accession-1','10-Q')")
        rows = (
            ("re", "us-gaap:StatementEquityComponentsAxis", "us-gaap:RetainedEarningsMember",
             "Retained Earnings", "segment net income: Retained Earnings"),
            ("cls", "us-gaap:StatementClassOfStockAxis", "us-gaap:CommonClassAMember",
             "Class A", "segment basic eps: Class A"),
            ("fp", "us-gaap:ContractWithCustomerBasisOfPricingAxis",
             "us-gaap:FixedPriceContractMember", "Fixed-price - Americas",
             "segment revenue: Fixed-price - Americas"),
            ("seg", "us-gaap:StatementBusinessSegmentsAxis", "synthetic:EuropeSegmentMember",
             "Europe", "segment revenue: Europe"),
            ("gone", "us-gaap:StatementEquityComponentsAxis", "us-gaap:RetainedEarningsMember",
             "Retained Earnings", "segment net income: Retained Earnings"),
        )
        for index, (key, axis, member, label, metric) in enumerate(rows):
            ref = f"claim-version:{key}"
            connection.execute("INSERT INTO coverage_mission_statement_lines VALUES(?,?,?,?,?,?,?)",
                               (f"i1#{index}", "i1", "us-gaap:Revenues" if key == "fp" else "us-gaap:NetIncomeLoss", label, axis,
                                member, None))
            connection.execute(
                "INSERT INTO quantitative_claim_promotions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"p:{key}", "company:synthetic:northvale", metric, "2031-01-01..2031-03-31",
                 "statement_line", f"i1#{index}", "1", "USD", "admitted", ref))
            connection.execute("INSERT INTO claim_versions VALUES(?,NULL)", (ref,))
        connection.execute("INSERT INTO claim_retirement_decisions VALUES"
                           "('claim-version:gone','retired')")
        connection.commit()
        connection.close()

    def test_the_replay_groups_what_the_rule_refuses_and_skips_the_retired(self) -> None:
        replay = _load_replay()
        result = replay.replay(self.state)
        self.assertEqual(result["admitted_segment_claims"], 5)
        self.assertEqual(result["already_retired"], 1)
        self.assertEqual(result["kept_as_segment"], 1)
        refs = {name: [item["claim_version_ref"] for item in group["claims"]]
                for name, group in result["groups"].items()}
        self.assertEqual(refs, {"equity": ["claim-version:re"],
                                "other": ["claim-version:cls"],
                                "renamed": ["claim-version:fp"]})
        self.assertEqual(result["groups"]["renamed"]["claims"][0]["new_metric"],
                         "revenue by pricing basis: Fixed-price - Americas")
        self.assertEqual(result["groups"]["equity"]["selection_sha256"],
                         replay.selection_hash(["claim-version:re"]))

    def test_the_replay_never_writes_the_core(self) -> None:
        replay = _load_replay()
        before = (self.state / "core.sqlite").read_bytes()
        out = self.state / "replay.json"
        with contextlib.redirect_stdout(io.StringIO()):
            replay.main(["--state-dir", str(self.state), "--out", str(out)])
        self.assertEqual((self.state / "core.sqlite").read_bytes(), before)
        self.assertEqual(json.loads(out.read_text())[0]["groups"]["equity"]["count"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
