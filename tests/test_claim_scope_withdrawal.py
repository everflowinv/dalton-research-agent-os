"""Synthetic reviewed selections preserve the withdrawal and replay boundary."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "fixtures" / "claim-scope-withdrawal" / "withdraw.sh"
LIST = ROOT / "tests" / "fixtures" / "claim-scope-withdrawal" / "review.md"
REF_RE = re.compile(r"claim-version:[0-9a-f]{64}")

INTERVIEW = ("Q: Since Northvale's CloudDesk cannot be widely used domestically due to compliance "
             "issues, do you think others could leverage this?\n\n"
             "A: They can't skip over traditional office software; they can only take over operations.")
PAGE_URL = "https://vendor.example.invalid/alternatives"


class WithdrawalScriptTests(unittest.TestCase):
    def test_the_script_is_dry_run_by_default_and_names_each_claim_once(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        if shutil.which("zsh"):
            subprocess.run(["zsh", "-n", str(SCRIPT)], check=True)
        self.assertIn("MODE=${1:-dry-run}", text)
        self.assertIn("ACTOR=${ACTOR:-human:owner}", text)
        self.assertIn("dalton_core.claim_admission_cli retire", text)
        refs = REF_RE.findall(text)
        self.assertEqual(len(refs), len(set(refs)), "a claim is listed twice")
        # 3 rereads + 1 speaker + 1 subject mismatch + 6 same-findings + 14 marketing, and 2 optional.
        self.assertEqual(len(refs), 27)
        # Only ``apply`` writes, and it goes through the writer with --apply.
        self.assertEqual(text.count("--apply --actor"), 1)
        listed = LIST.read_text(encoding="utf-8")
        for ref in refs:
            head, tail = ref.split(":")[1][:8], ref[-8:]
            self.assertTrue(head in listed or tail in listed, ref)

    def test_each_group_is_held_to_its_reviewed_hash_and_run_from_a_release(self) -> None:
        from dalton_core.claim_admission_cli import selection_hash

        text = SCRIPT.read_text(encoding="utf-8")
        # The owner runs a release's venv, never the development checkout's.
        self.assertNotIn(".venv/bin/python", text)
        self.assertIn("release_path", text)
        self.assertIn("DALTON_WORKSPACE_MANIFEST=$WS_MANIFEST", text)
        groups = {"A": "A_REFS", "B1": "B1_REFS", "B2": "B2_REFS", "C": "C_REFS", "D": "D_REFS",
                  "X_ENV_A": "X_ENV_A_REFS", "X_ENV_B": "X_ENV_B_REFS"}
        listed = LIST.read_text(encoding="utf-8")
        for name, array in groups.items():
            body = re.search(array + r"=\((.*?)\n\)", text, re.S).group(1)
            expected = re.search(r"SEL_" + name + r"=\$\{SEL_" + name + r":-([0-9a-f]{64})\}",
                                 text).group(1)
            self.assertEqual(expected, selection_hash(REF_RE.findall(body)), name)
            self.assertIn(expected[:8], listed, name)
            self.assertRegex(text, r'run_group "[^"]+" "\$[A-Z0-9_]+" "\$[A-Z0-9_]+" "\$SEL_' + name + r'" \$'
                             + array)
        # Both the dry run and the apply pass the reviewed hash.
        self.assertEqual(text.count('--expect-selection "$SEL"'), 2)


def _core(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, claim_json TEXT, created_at TEXT);
        CREATE TABLE evidence_versions(evidence_version_id TEXT PRIMARY KEY, evidence_json TEXT);
        CREATE TABLE evidence_relations(claim_version_id TEXT, evidence_version_id TEXT, relation TEXT);
        CREATE TABLE transcript_claim_citation_bindings(binding_id TEXT PRIMARY KEY,
            correction_set_version_ref TEXT, source_content_hash TEXT, source_start INT, source_end INT);
        CREATE TABLE transcript_correction_set_versions(version_id TEXT PRIMARY KEY, record_json TEXT);
    """)
    return connection


def _claim(connection, n, *, digest, document_ref, start, end, statement, created, subject="company:synthetic:northvale"):
    ref = f"claim-version:{n:064x}"
    binding = f"transcript-claim-citation-binding:{n:032x}"
    correction = f"transcript-correction-set-version:{n:032x}"
    connection.execute("INSERT INTO claim_versions VALUES(?,?,?)", (ref, json.dumps({
        "claim_kind": "qualitative", "subject_ref": subject, "normalized_statement": statement,
        "period": "2031"}), created))
    connection.execute("INSERT INTO evidence_versions VALUES(?,?)", (f"ev:{n}", json.dumps(
        {"artifact_refs": [{"ref": binding}]})))
    connection.execute("INSERT INTO evidence_relations VALUES(?,?,'supports')", (ref, f"ev:{n}"))
    connection.execute("INSERT INTO transcript_claim_citation_bindings VALUES(?,?,?,?,?)",
                       (binding, correction, digest, start, end))
    connection.execute("INSERT INTO transcript_correction_set_versions VALUES(?,?)",
                       (correction, json.dumps({"document_ref": document_ref})))
    return ref


class ReplayTests(unittest.TestCase):
    def test_the_replay_finds_every_kind_the_list_is_made_of(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts"))
        from replay_claim_scope_2026_09_29 import replay

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            connection = _core(state / "core.sqlite")
            interview = hashlib.sha256(INTERVIEW.encode()).hexdigest()
            spool = state / "connector-spool" / "connector-spool" / "objects" / interview[:2]
            spool.mkdir(parents=True)
            (spool / interview).write_text(INTERVIEW, encoding="utf-8")
            excerpt = "guidepoint-excerpt:sha256:" + interview
            first = _claim(connection, 1, digest=interview, document_ref=excerpt, start=INTERVIEW.index("A:") + 3, end=len(INTERVIEW),
                           statement="The respondent said office software cannot be skipped.",
                           created="2031-02-03T11:32:00+00:00")
            again = _claim(connection, 2, digest=interview, document_ref=excerpt, start=INTERVIEW.index("A:") + 3, end=len(INTERVIEW),
                           statement="An interviewee stated traditional office software can't be skipped.",
                           created="2031-02-03T12:31:00+00:00")
            question = _claim(connection, 3, digest=interview, document_ref=excerpt, start=0, end=INTERVIEW.index("\n\n"),
                              statement="A respondent said CloudDesk cannot be used in a restricted region.",
                              created="2031-02-03T11:29:00+00:00")
            page = "public-web-document:url-sha256:" + hashlib.sha256(PAGE_URL.encode()).hexdigest() \
                + ":body-sha256:" + "e" * 64
            vendor = _claim(connection, 4, digest="d" * 64, document_ref=page, start=0, end=10,
                            statement="Northvale bills by the day.", created="2031-02-03T12:29:00+00:00")
            filing = "f" * 64
            restated = [_claim(connection, 10 + n, digest=filing, document_ref="sec", start=n * 100,
                               end=n * 100 + 50, statement=text, created=f"2031-02-03T12:1{n}:00+00:00")
                        for n, text in enumerate((
                            "Northvale Research describes itself as a leading global provider of digital engineering "
                            "and cloud services.",
                            "Northvale Research describes itself as a leading global provider of digital "
                            "engineering and cloud services.",
                            "Northvale's Americas revenue grew in 2030 driven by financial services clients.",
                            "Northvale's top five clients declined as a share of revenue in 2030."))]
            connection.commit()
            connection.close()
            result = replay(state, "2031-02-03T11:00", (PAGE_URL,))
        self.assertEqual([(item["claim_version_ref"], item["duplicate_of"])
                          for item in result["same_span_rereads"]], [(again, first)])
        self.assertEqual([item["claim_version_ref"] for item in result["interviewer_turn"]], [question])
        self.assertEqual([item["claim_version_ref"] for item in result["marketing_url"]], [vendor])
        groups = {group["keep"]: [w["claim_version_ref"] for w in group["withdraw"]]
                  for group in result["same_finding_in_read"]}
        self.assertEqual(groups[restated[0]], [restated[1]])
        [scope] = [item for item in result["scope_replay"] if item["admitted_since"] == 4]
        self.assertEqual(scope["decisions"], {"admit": 3, "duplicate:same_finding": 1})


if __name__ == "__main__":
    unittest.main()
