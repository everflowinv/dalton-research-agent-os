"""No banned term in any user-visible string (batch am, 2026-10-09).

Owner: 「要从源头上解决问题，确保以后在新的环境下研究新的公司时不再出现问题」.
The vocabulary lives in ``dalton_core.terminology``; this lint makes sure no
display module, cockpit page or server-side Chinese template brings an
engineering word (规划器, 车道, 工单, 再买一次, SHA-256 ...) back.

What is scanned
---------------
* Python display modules (:data:`PY_DISPLAY_MODULES`): every string constant
  that is not a docstring, including the literal parts of f-strings.  A CJK
  banned term counts in any string; an ASCII one (``SHA-256``, ``sha256:``,
  ``--apply``, ``rule:``) only in a string that also holds Chinese -- an
  English log line or a regex is not analyst text.
* The cockpit's JavaScript and HTML (:data:`JS_ROOTS`): string literals
  (quotes and template literals) after comments are removed, with the same
  CJK rule; HTML text outside comments.

What is not
-----------
* The administrator page (``cockpit_v2/admin/``) -- it is for the technical
  administrator and keeps 模型网关 / 档位 / 链尾 on purpose.
* The registry itself and its generated export (``charts/vocabulary.js``).
* A string whose line (or the line above) carries ``term-ok: <reason>`` --
  for technical-details blocks and administrator-only fields.
* :data:`dalton_core.terminology.LINT_ALLOWLIST` -- counted exceptions; the
  lint fails when a file has more occurrences than allowed (a new one) and
  when it has fewer (the allowlist must shrink with the code).
"""

from __future__ import annotations

import ast
import re
import unittest
from collections import Counter
from pathlib import Path

from dalton_core import terminology
from dalton_core.terminology import BANNED_TERMS, LINT_ALLOWLIST

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "dalton_core"
CJK = re.compile(r"[㐀-鿿]")
MARKER = "term-ok"

#: Server modules whose strings reach an analyst page, Feishu or the brief.
PY_DISPLAY_MODULES = (
    "needs_human.py", "needs_human_ack.py", "cockpit_owner_doors.py", "owner_door_jobs.py",
    "owner_delivery.py", "cockpit_weekly_brief.py", "weekly_brief.py", "cockpit_plane.py",
    "financial_display.py", "cockpit_model_display.py", "company_scenario.py", "cockpit_scenarios.py",
    "cockpit_conviction.py", "cockpit_research_library.py", "cockpit_model.py",
    "cockpit_return_attribution.py", "cockpit_owner_levers.py", "cockpit_fund_templates.py",
    "cockpit_verifier_card.py", "cockpit_ask_recovery.py", "cockpit_setup.py",
    "data_foundation.py", "research_gap_display.py", "numeric_display.py", "research_html_export.py",
    "mission_stage.py", "research_planner.py", "event_judgement.py",
    "cockpit_v2/charts_api.py", "cockpit_v2/evidence_api.py", "cockpit_v2/evidence_search_api.py",
    "cockpit_v2/scenario_api.py", "cockpit_v2/feedback_data.py",
    # 2026-10-09 (batch B follow-up): the valuation modules' missing-data
    # reasons reach the numbers pack and the company page.
    "valuation_snapshot.py", "valuation_snapshot_lane.py", "valuation_snapshot_cli.py",
    "valuation_forward.py", "valuation_history.py", "valuation_fcf_bridge.py",
    # Batch E: the material acquisition service's cards, lane summary and cockpit page.
    "material_acquisition.py", "material_acquisition_lane.py", "cockpit_v2/material_requests_api.py",
    # 2026-10-09 (batch an): the 研究一致性 block's conflict and stale texts.
    "product_consistency.py",
    "conviction_policy.py", "final_text_state.py", "event_judgement_result.py",
)

#: Front-end roots: every .js / .mjs / .html below them (admin excluded).
JS_ROOTS = ("cockpit_v2",)
HTML_FILES = ("cockpit_control.html", "cockpit_weekly_brief.html")
EXCLUDED = ("cockpit_v2/admin/", "cockpit_v2/charts/vocabulary.js")


def _ascii(term: str) -> bool:
    return not CJK.search(term)


def _hits_in(text: str) -> list[str]:
    has_cjk = bool(CJK.search(text))
    out = []
    for term in BANNED_TERMS:
        if _ascii(term.term) and term.pattern is None and not has_cjk:
            continue
        if term.pattern is not None and not has_cjk and not CJK.search(term.pattern):
            continue
        out.extend([term.term] * len(term.search(text)))
    return out


def _marked(lines: list[str], lineno: int) -> bool:
    for n in (lineno - 1, lineno - 2):
        if 0 <= n < len(lines) and MARKER in lines[n]:
            return True
    return False


def python_hits(path: Path) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if _marked(lines, node.lineno) or (getattr(node, "end_lineno", None) and _marked(lines, node.end_lineno)):
                continue
            for term in _hits_in(node.value):
                out.append((node.lineno, term, node.value[:120]))
    return out


_JS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT = re.compile(r"(^|[\s;,{}()])//[^\n]*")
_JS_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`', re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_SPLIT_SCRIPT = re.compile(r"<script\b.*?</script>|(?:(?!<script\b).)+", re.S | re.I)
_TAGS = re.compile(r"<[^>]*>")
_TEMPLATE_CODE = re.compile(r"\$\{(?:[^{}]|\{[^{}]*\})*\}")


def _blank(match: re.Match) -> str:
    # Keep line numbers: replace a comment by its newlines.
    return re.sub(r"[^\n]", " ", match.group(0))


def js_hits(path: Path) -> list[tuple[int, str, str]]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    text = _HTML_COMMENT.sub(_blank, source) if path.suffix == ".html" else source
    text = _JS_COMMENT.sub(_blank, text)
    text = _JS_LINE_COMMENT.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), text)
    out = []
    if path.suffix == ".html":
        # Markup text line by line; inline <script> through its string literals.
        markup = _SCRIPT.sub(_blank, text)
        for lineno, line in enumerate(markup.splitlines(), 1):
            if _marked(lines, lineno + 1):
                continue
            for term in _hits_in(_TAGS.sub(" ", line)):
                out.append((lineno, term, line.strip()[:120]))
        text = "".join(m.group(0) if m.group(0).lower().startswith("<script") else _blank(m)
                       for m in _SPLIT_SCRIPT.finditer(text))
    for match in _JS_STRING.finditer(text):
        lineno = text.count("\n", 0, match.start()) + 1
        if _marked(lines, lineno + 1) or _marked(lines, lineno):
            continue
        # A template literal's ${...} parts are code, not text.
        literal = _TEMPLATE_CODE.sub(" ", match.group(0)) if match.group(0).startswith("`") else match.group(0)
        for term in _hits_in(literal):
            out.append((lineno, term, match.group(0)[:120]))
    return out


def front_end_files() -> list[Path]:
    files = []
    for root in JS_ROOTS:
        for path in sorted((CORE / root).rglob("*")):
            rel = path.relative_to(CORE).as_posix()
            if path.suffix in {".js", ".mjs", ".html"} and not rel.startswith(EXCLUDED):
                files.append(path)
    files.extend(CORE / name for name in HTML_FILES)
    return files


def inventory() -> dict[str, list[tuple[int, str, str]]]:
    out: dict[str, list[tuple[int, str, str]]] = {}
    for name in PY_DISPLAY_MODULES:
        path = CORE / name
        if path.exists():
            out[name] = python_hits(path)
    for path in front_end_files():
        out[path.relative_to(CORE).as_posix()] = js_hits(path)
    return out


class TerminologyLintTests(unittest.TestCase):
    def test_no_banned_term_in_user_visible_strings(self):
        problems = []
        seen_keys = set()
        for rel, hits in inventory().items():
            counts = Counter(term for _, term, _ in hits)
            for term, count in counts.items():
                allowed, _reason = LINT_ALLOWLIST.get((rel, term), (0, ""))
                seen_keys.add((rel, term))
                if count > allowed:
                    where = "; ".join(f"L{n}: {t!r}" for n, x, t in hits if x == term)[:600]
                    replacement = next(b.replacement for b in BANNED_TERMS if b.term == term)
                    problems.append(f"{rel}: 「{term}」 ×{count} (allowed {allowed}) -> 「{replacement}」  {where}")
                elif count < allowed:
                    problems.append(f"{rel}: 「{term}」 allowlisted ×{allowed} but only ×{count} left: lower LINT_ALLOWLIST")
        for key, (allowed, _reason) in LINT_ALLOWLIST.items():
            if key not in seen_keys and allowed:
                problems.append(f"{key[0]}: 「{key[1]}」 allowlisted but gone: remove it from LINT_ALLOWLIST")
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_every_allowlist_entry_has_a_reason(self):
        for key, (count, reason) in LINT_ALLOWLIST.items():
            self.assertGreater(count, 0, key)
            self.assertTrue(reason.strip(), key)
            self.assertIn(key[1], {t.term for t in BANNED_TERMS}, key)

    def test_the_lint_catches_a_new_occurrence(self):
        # Canaries: a banned term in a new string of each kind is found.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            py = Path(tmp) / "x.py"
            py.write_text('def f():\n    """车道 docstring is fine."""\n    return f"研究车道{1}" + "再买一次"\n', encoding="utf-8")
            self.assertEqual(sorted(t for _, t, _ in python_hits(py)), ["再买一次", "车道"])
            js = Path(tmp) / "x.js"
            js.write_text('// 车道 in a comment is fine\nconst a = `规划器 ${x}`; const b = "原文 SHA-256：";\n'
                          'const ok = "SHA-256 only in English";\nconst marked = "车道"; // term-ok: admin\n', encoding="utf-8")
            self.assertEqual(sorted(t for _, t, _ in js_hits(js)), ["SHA-256", "规划器"])
            self.assertEqual([t for _, t, _ in python_hits(self._write(tmp, 'X = "rule: growth < 0.03（营业收入）"\n'))],
                             ["rule: …"])

    @staticmethod
    def _write(tmp: str, text: str) -> Path:
        path = Path(tmp) / "y.py"
        path.write_text(text, encoding="utf-8")
        return path

    def test_scope_names_real_files(self):
        for name in PY_DISPLAY_MODULES + HTML_FILES:
            self.assertTrue((CORE / name).exists(), name)
        self.assertTrue(terminology.JS_MODULE_PATH.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
