"""Keep controlled completions off pre-bound transports without control proofs.

OpenClaw 2026.9.3 can attach a provider-specific completion transport to a
prepared model.  The Google transport attached in that path does not implement
providerControls, even though the native @openclaw/ai transport does.  A
controlled request must therefore enter the default runtime without carrying
the host binding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

SUPPORTED_VERSION = "2026.9.3"
ORIGINAL = """\tlet completionModel = getModelCompletionTransport(params.model) ?? prepareModelForSimpleCompletion({
\t\tapiRegistry: runtime?.registry ?? defaultApiRegistry,
\t\tmodel: params.model,
\t\tcfg: params.cfg
\t});"""
PATCHED = """\tconst controlledTransport = params.options?.providerControls !== void 0;
\tconst boundCompletionTransport = getModelCompletionTransport(params.model);
\tlet completionModel = controlledTransport ? { ...params.model } : boundCompletionTransport ?? prepareModelForSimpleCompletion({
\t\tapiRegistry: runtime?.registry ?? defaultApiRegistry,
\t\tmodel: params.model,
\t\tcfg: params.cfg
\t});
\tif (controlledTransport && getModelLlmRuntime(completionModel)) throw new Error("Controlled completion retained a host-bound transport");"""
ORIGINAL_BIND = "\tif (runtime) completionModel = bindModelLlmRuntime(completionModel, runtime);"
PATCHED_BIND = "\tif (runtime && !controlledTransport) completionModel = bindModelLlmRuntime(completionModel, runtime);"


def target(root: Path) -> Path:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != SUPPORTED_VERSION:
        raise ValueError(
            f"controlled transport patch supports OpenClaw {SUPPORTED_VERSION}, "
            f"found {package.get('version')!r}"
        )
    matches = sorted((root / "dist").glob("simple-completion-execution-*.mjs"))
    if len(matches) != 1:
        raise ValueError(f"expected one simple completion bundle, found {len(matches)}")
    return matches[0]


def apply(root: Path, *, check: bool) -> bool:
    path = target(root)
    source = path.read_text(encoding="utf-8")
    original_count = source.count(ORIGINAL)
    patched_count = source.count(PATCHED)
    original_bind_count = source.count(ORIGINAL_BIND)
    patched_bind_count = source.count(PATCHED_BIND)
    if patched_count == 1 and patched_bind_count == 1:
        if original_count or original_bind_count:
            raise ValueError("controlled transport patch has duplicate original anchors")
        return False
    if patched_count or patched_bind_count:
        raise ValueError("controlled transport patch is partial or duplicated")
    if original_count != 1:
        raise ValueError("OpenClaw completion transport anchor changed")
    if original_bind_count != 1:
        raise ValueError("OpenClaw completion runtime binding anchor changed")
    if check:
        raise ValueError("controlled completion transport patch is missing")
    source = source.replace(ORIGINAL, PATCHED, 1)
    source = source.replace(ORIGINAL_BIND, PATCHED_BIND, 1)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".mjs", dir=path.parent,
        prefix=f".{path.name}.", delete=False,
    ) as handle:
        candidate = Path(handle.name)
        handle.write(source)
    try:
        checked = subprocess.run(
            ["node", "--check", str(candidate)], text=True,
            capture_output=True, check=False,
        )
        if checked.returncode != 0:
            raise ValueError("patched OpenClaw completion bundle failed syntax validation")
        os.chmod(candidate, path.stat().st_mode)
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)
    return True


def source_sha256(root: Path) -> str:
    return hashlib.sha256(target(root).read_bytes()).hexdigest()


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
    print("PATCH_CHANGED" if changed else "OK controlled completion transport")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
