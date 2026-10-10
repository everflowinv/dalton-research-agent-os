"""ws-7d's prior-research link becomes a directory of its own (owner, 2026-09-30)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.prior_research_core import document_ref
from dalton_core.store import DaltonStore
from dalton_core.workspace import create_workspace_manifest


class LocalizePriorResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "localize_prior", Path(__file__).resolve().parents[1]
            / "scripts" / "localize_workspace_prior_research.py")
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        release = root / "release"; release.mkdir()
        self.fleet = root / "fleet" / "workspaces"
        workspace = create_workspace_manifest(
            root / "fleet", "ws-prior", 18977, "release:sha256:" + "e" * 64, release,
            shared_readonly_paths=[release])
        self.state = workspace.state_dir
        self.state.mkdir(parents=True, exist_ok=True)
        # The legacy corpus: two companies, three documents, one referenced.
        self.legacy = root / "legacy" / "feeds" / "prior-research"
        for company, names in (("0000000001", ["memo.md", "notes.md"]), ("0000000002", ["x.md"])):
            folder = self.legacy / company
            folder.mkdir(parents=True)
            for name in names:
                (folder / name).write_text(f"{company} {name}\n")
            (folder / "manifest.json").write_text(json.dumps({"documents": [
                {"path": name, "kind": "memo", "as_of": "2026-01-01"}
                for name in names]}))
        (self.state / "feeds").mkdir(parents=True)
        os.symlink(self.legacy, self.state / "feeds" / "prior-research")
        self.referenced = document_ref("0000000001", "memo.md")
        with DaltonStore(str(self.state / "core.sqlite")) as store:
            store.connection.execute("CREATE TABLE fixture_refs(record_json TEXT)")
            store.connection.execute("INSERT INTO fixture_refs VALUES (?)",
                                     (json.dumps({"document_ref": self.referenced}),))
            store.connection.commit()

    def test_dry_run_lists_only_referenced_documents_and_writes_nothing(self) -> None:
        plan = self.script.build_plan("ws-prior", self.fleet)
        self.assertEqual(plan["status"], "linked")
        self.assertEqual(plan["companies"], ["0000000001"])
        self.assertEqual([item["path"] for item in plan["files"]], ["memo.md"])
        self.assertGreater(plan["source_du_bytes"], plan["copy_bytes"] - 200)
        self.assertTrue((self.state / "feeds" / "prior-research").is_symlink())

    def test_apply_swaps_the_link_for_a_real_directory_and_keeps_the_link(self) -> None:
        plan = self.script.build_plan("ws-prior", self.fleet)
        with patch.object(self.script, "cockpit_port_busy", return_value=False):
            result = self.script.apply(plan)
        self.assertEqual(result["status"], "localized")
        target = self.state / "feeds" / "prior-research"
        self.assertTrue(target.is_dir() and not target.is_symlink())
        self.assertEqual((target / "0000000001" / "memo.md").read_text(), "0000000001 memo.md\n")
        manifest = json.loads((target / "0000000001" / "manifest.json").read_text())
        self.assertEqual([item["path"] for item in manifest["documents"]], ["memo.md"])
        backup = self.state / "feeds" / "prior-research.legacy-link-20260930"
        self.assertTrue(backup.is_symlink())
        self.assertTrue((self.legacy / "0000000001" / "notes.md").is_file())  # source untouched
        # The document id is resolvable in the new corpus.
        from dalton_core.prior_research_core import company_folders, manifest_for
        entries, _ = manifest_for(target, "0000000001")
        self.assertEqual({document_ref("0000000001", item["path"]) for item in entries},
                         {self.referenced})
        self.assertEqual(company_folders(target), ["0000000001"])
        again = self.script.build_plan("ws-prior", self.fleet)
        self.assertEqual(again["status"], "already_local")

    def test_apply_refuses_while_the_services_run(self) -> None:
        plan = self.script.build_plan("ws-prior", self.fleet)
        with patch.object(self.script, "cockpit_port_busy", return_value=True):
            with self.assertRaises(self.script.LocalizeError):
                self.script.apply(plan)
        self.assertTrue((self.state / "feeds" / "prior-research").is_symlink())


if __name__ == "__main__":
    unittest.main()
