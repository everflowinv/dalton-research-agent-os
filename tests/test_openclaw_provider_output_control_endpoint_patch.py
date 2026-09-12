import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from integrations.openclaw_host_patches.patch_provider_output_control_endpoint import (
    CODEX_SANITIZER_MARKERS,
    PATCHED,
    apply,
)


class ProviderOutputControlEndpointPatchTests(unittest.TestCase):
    def _root(self, source: str) -> tuple[tempfile.TemporaryDirectory, Path]:
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        (root / "dist").mkdir()
        ai_dist = root / "node_modules" / "@openclaw" / "ai" / "dist"
        ai_dist.mkdir(parents=True)
        (root / "package.json").write_text(
            json.dumps({"version": "2026.9.3"}), encoding="utf-8"
        )
        (root / "dist" / "runtime-llm.runtime-test.mjs").write_text(
            source, encoding="utf-8"
        )
        (ai_dist / "transports.mjs").write_text(
            "\n".join(CODEX_SANITIZER_MARKERS), encoding="utf-8"
        )
        return temp, root

    def test_patch_is_idempotent_and_syntax_valid(self):
        from integrations.openclaw_host_patches.patch_provider_output_control_endpoint import ORIGINAL

        temp, root = self._root(f"async function f(params, prepared) {{\n{ORIGINAL}\n}}\n")
        with temp:
            self.assertTrue(apply(root, check=False))
            self.assertFalse(apply(root, check=False))
            self.assertFalse(subprocess.run(
                ["node", "--check", str(next((root / "dist").glob("*.mjs")))],
                check=False,
            ).returncode)

    def test_patch_refuses_changed_codex_sanitizer_contract(self):
        from integrations.openclaw_host_patches.patch_provider_output_control_endpoint import ORIGINAL

        temp, root = self._root(f"async function f(params, prepared) {{\n{ORIGINAL}\n}}\n")
        with temp:
            transports = root / "node_modules" / "@openclaw" / "ai" / "dist" / "transports.mjs"
            transports.write_text("contract changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sanitizer contract changed"):
                apply(root, check=False)

    def test_resolved_endpoint_matrix(self):
        body = PATCHED.replace(
            '\t\tif ("error" in prepared) throw new Error(`Plugin LLM completion failed: ${prepared.error}`);',
            '',
        )
        source = f'''function createLlmCompleteError(code, message) {{ const e = new Error(message); e.code = code; return e; }}
async function check(params, prepared) {{
{body}
return "dispatched";
}}
const cases = JSON.parse(process.argv[2]);
const out = [];
for (const item of cases) {{
  try {{ out.push(await check(item.params, {{model:item.model}})); }}
  catch (error) {{ out.push({{code:error.code, message:error.message}}); }}
}}
process.stdout.write(JSON.stringify(out));
'''
        cases = [
            ({"api": "openai-responses", "baseUrl": "https://api.openai.com/v1"}, True),
            ({"api": "openclaw-openai-chatgpt-responses-transport", "baseUrl": "https://chatgpt.com/backend-api/codex/responses"}, False),
            ({"api": "openai-responses", "baseUrl": "https://api.openai.com.evil/v1"}, False),
            ({"api": "openai-responses", "baseUrl": "https://user@api.openai.com/v1"}, False),
            ({"api": "openai-responses", "baseUrl": "https://api.openai.com:444/v1"}, False),
            ({"api": "openai-responses", "baseUrl": "https://proxy.example/v1"}, False),
            ({"api": "openai-responses"}, False),
        ]
        payload = [{"params": {"providerControls": {"mode": "openai-responses-input-count-v1"}}, "model": model} for model, _ in cases]
        payload += [{"params": {"providerControls": {"mode": "google-generative-ai-count-tokens-v1"}}, "model": {"api": "google-generative-ai", "baseUrl": "https://example.invalid"}}]
        completed = subprocess.run(
            ["node", "--input-type=module", "-", json.dumps(payload)],
            input=source, text=True, capture_output=True, check=True,
        )
        observed = json.loads(completed.stdout)
        self.assertEqual([item == "dispatched" for item in observed[:-1]], [ok for _, ok in cases])
        self.assertEqual(observed[-1], "dispatched")
        for item in observed[1:-1]:
            self.assertEqual(item["code"], "REQUIRED_CONTROLS_UNAVAILABLE")
            self.assertEqual(
                item["message"],
                "Plugin LLM completion failed: selected endpoint cannot enforce "
                "provider max_output_tokens.",
            )


if __name__ == "__main__":
    unittest.main()
