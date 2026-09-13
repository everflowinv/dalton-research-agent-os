from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"


class CockpitLanguageTests(unittest.TestCase):
    def test_frontend_language_contract(self) -> None:
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "check_cockpit_language.js")],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_javascript_parses(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        javascript = text.split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run(
            ["node", "--check", "-"],
            input=javascript,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_unknown_backend_text_is_not_written_as_a_primary_error(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertNotIn("textContent=e.message", text)
        self.assertNotIn("node(\"div\",e.message", text)
        self.assertIn("technicalDetails(p.technical)", text)

    def test_pagination_keeps_legacy_response_compatible(self) -> None:
        text = HTML.read_text(encoding="utf-8")
        self.assertIn("r.returned_count??r.items.length", text)
        self.assertIn("if(r.next_cursor!=null||claimPages.length)", text)


if __name__ == "__main__":
    unittest.main()
