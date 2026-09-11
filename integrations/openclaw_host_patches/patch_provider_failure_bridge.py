"""Preserve returned retryable HTTP failures across the OpenClaw LLM facade.

The provider transport already projects an HTTP response into an assistant
result with ``stopReason == "error"`` and a numeric-string ``errorCode``.  The
2026.9.3 plugin facade currently drops that result by returning an empty text
completion.  This exact-version patch exports only a closed, non-sensitive
failure union for HTTP 429 and 5xx responses.  Exceptions and other errors keep
their existing conservative path.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile

SUPPORTED_VERSION = "2026.9.3"
ORIGINAL = '''\t\tif (params.providerControls && !providerControlProof) throw new Error("Plugin LLM completion failed: provider controls were not enforced by the selected transport.");
\t\tconst text = result.content.filter((c) => c.type === "text").map((c) => c.text).join("");'''
PATCHED = '''\t\tif (params.providerControls && !providerControlProof) throw new Error("Plugin LLM completion failed: provider controls were not enforced by the selected transport.");
\t\tconst returnedHttpStatus = result.stopReason === "error" && typeof result.errorCode === "string" && /^(?:429|5\\d\\d)$/.test(result.errorCode) ? Number(result.errorCode) : void 0;
\t\tif (returnedHttpStatus !== void 0) return finalizePluginLlmCompletion({
\t\t\tcfg,
\t\t\thostPluginId: pluginPolicyId,
\t\t\tsuppressUsage: false,
\t\t\trawUsage: result.usage,
\t\t\tlogger,
\t\t\tresult: {
\t\t\t\tfailure: { version: "0.1", state: "provider_completed_failure", httpStatus: returnedHttpStatus },
\t\t\t\tprovider: prepared.selection.provider,
\t\t\t\tmodel: prepared.selection.modelId,
\t\t\t\tagentId,
\t\t\t\texecution: { mode: "direct-provider", owner: { kind: "provider", id: prepared.selection.provider } },
\t\t\t\taudit
\t\t\t}
\t\t});
\t\tconst text = result.content.filter((c) => c.type === "text").map((c) => c.text).join("");'''


def target(root: Path) -> Path:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != SUPPORTED_VERSION:
        raise ValueError(f"provider failure bridge supports OpenClaw {SUPPORTED_VERSION}")
    matches = sorted((root / "dist").glob("runtime-llm.runtime-*.mjs"))
    if len(matches) != 1:
        raise ValueError(f"expected one runtime LLM bundle, found {len(matches)}")
    return matches[0]


def apply(root: Path, *, check: bool) -> bool:
    path = target(root)
    source = path.read_text(encoding="utf-8")
    original_count, patched_count = source.count(ORIGINAL), source.count(PATCHED)
    if patched_count == 1:
        if original_count:
            raise ValueError("provider failure bridge contains its original anchor")
        return False
    if patched_count:
        raise ValueError("provider failure bridge is duplicated")
    if original_count != 1:
        raise ValueError("OpenClaw provider failure bridge anchor changed")
    if check:
        raise ValueError("provider failure bridge is missing")
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
    print("PATCH_CHANGED" if changed else "OK provider failure bridge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
