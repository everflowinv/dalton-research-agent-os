"""Reject controlled OpenAI calls whose resolved endpoint drops the output cap.

OpenClaw 2026.9.3 builds ``max_output_tokens`` for Responses requests, but its
Codex/ChatGPT backend sanitizer intentionally removes that field.  The generic
plugin capability is global, so the broker cannot see this per-route mismatch.
This exact-version host patch checks the resolved API and base URL after model
selection and before transport dispatch.  Unknown endpoints fail closed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

SUPPORTED_VERSION = "2026.9.3"
ORIGINAL = '''\t\tif ("error" in prepared) throw new Error(`Plugin LLM completion failed: ${prepared.error}`);'''
PATCHED = '''\t\tif ("error" in prepared) throw new Error(`Plugin LLM completion failed: ${prepared.error}`);
        if (params.providerControls?.mode === "openai-responses-input-count-v1") {
            const resolvedApi = prepared.model.api;
            const resolvedBaseUrl = typeof prepared.model.baseUrl === "string" ? prepared.model.baseUrl.trim() : "";
            let supportsProviderOutputLimit = false;
            try {
                const endpoint = new URL(resolvedBaseUrl);
                supportsProviderOutputLimit = resolvedApi === "openai-responses"
                    && endpoint.protocol === "https:"
                    && endpoint.hostname === "api.openai.com"
                    && endpoint.port === ""
                    && endpoint.username === ""
                    && endpoint.password === "";
            } catch {}
            if (!supportsProviderOutputLimit) throw createLlmCompleteError("REQUIRED_CONTROLS_UNAVAILABLE", "Plugin LLM completion failed: selected endpoint cannot enforce provider max_output_tokens.");
        }'''

CODEX_SANITIZER_MARKERS = (
    'const OPENAI_CODEX_RESPONSES_UNSUPPORTED_PARAMS = [',
    '"max_output_tokens",',
    'function sanitizeOpenAICodexResponsesParams(model, params)',
    'for (const key of OPENAI_CODEX_RESPONSES_UNSUPPORTED_PARAMS) delete params[key];',
)


def assert_codex_transport_contract(root: Path) -> None:
    matches = sorted((root / "node_modules" / "@openclaw" / "ai" / "dist").glob(
        "transports.mjs"
    ))
    if len(matches) != 1:
        raise ValueError(f"expected one OpenClaw AI transports bundle, found {len(matches)}")
    source = matches[0].read_text(encoding="utf-8")
    if any(marker not in source for marker in CODEX_SANITIZER_MARKERS):
        raise ValueError("OpenClaw Codex max_output_tokens sanitizer contract changed")


def target(root: Path) -> Path:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != SUPPORTED_VERSION:
        raise ValueError(f"provider output control endpoint patch supports OpenClaw {SUPPORTED_VERSION}")
    matches = sorted((root / "dist").glob("runtime-llm.runtime-*.mjs"))
    if len(matches) != 1:
        raise ValueError(f"expected one runtime LLM bundle, found {len(matches)}")
    return matches[0]


def apply(root: Path, *, check: bool) -> bool:
    assert_codex_transport_contract(root)
    path = target(root)
    original_bytes = path.read_bytes()
    source = original_bytes.decode("utf-8")
    original_count, patched_count = source.count(ORIGINAL), source.count(PATCHED)
    if patched_count == 1:
        if original_count != 1:
            raise ValueError("provider output endpoint patch anchor is ambiguous")
        return False
    if patched_count:
        raise ValueError("provider output endpoint patch is duplicated")
    if original_count != 1:
        raise ValueError("OpenClaw provider output endpoint anchor changed")
    if check:
        raise ValueError("provider output endpoint patch is missing")
    candidate = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".mjs",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as handle:
            candidate = Path(handle.name)
            handle.write(source.replace(ORIGINAL, PATCHED, 1))
        checked = subprocess.run(["node", "--check", str(candidate)], text=True,
                                 capture_output=True, check=False)
        if checked.returncode:
            raise ValueError("patched runtime LLM bundle failed syntax validation")
        if path.read_bytes() != original_bytes:
            raise ValueError("OpenClaw runtime LLM bundle changed during patch validation")
        os.chmod(candidate, path.stat().st_mode)
        os.replace(candidate, path)
    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openclaw-root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        root_arg = args.openclaw_root or os.environ.get("OPENCLAW_INSTALL_ROOT")
        if not root_arg:
            raise ValueError("--openclaw-root or OPENCLAW_INSTALL_ROOT is required")
        changed = apply(Path(root_arg).resolve(), check=args.check)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print("PATCH_CHANGED" if changed else "OK provider output control endpoint")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
