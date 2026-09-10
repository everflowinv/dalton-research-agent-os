"""Keep controlled completions off pre-bound transports without control proofs.

OpenClaw 2026.9.3 can attach a provider-specific completion transport to a
prepared model.  The Google transport attached in that path does not implement
providerControls, even though the native @openclaw/ai transport does.  A
controlled request must therefore prepare the native transport explicitly.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    if PATCHED in source:
        if PATCHED_BIND not in source:
            raise ValueError("controlled transport patch is incomplete")
        return False
    if source.count(ORIGINAL) != 1:
        raise ValueError("OpenClaw completion transport anchor changed")
    if source.count(ORIGINAL_BIND) != 1:
        raise ValueError("OpenClaw completion runtime binding anchor changed")
    if check:
        raise ValueError("controlled completion transport patch is missing")
    source = source.replace(ORIGINAL, PATCHED, 1)
    source = source.replace(ORIGINAL_BIND, PATCHED_BIND, 1)
    path.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openclaw-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        changed = apply(args.openclaw_root.resolve(), check=args.check)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print("PATCH_CHANGED" if changed else "OK controlled completion transport")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
