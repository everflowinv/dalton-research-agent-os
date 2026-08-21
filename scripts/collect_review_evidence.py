#!/usr/bin/env python3
"""Collect review evidence from an argv-only, fail-closed manifest.

The collector never invokes a shell.  It validates every selected file and
command before atomically publishing the Markdown bundle.  Missing or empty
files, path escapes, failed/empty commands, timeouts, and oversized evidence
all leave no output artifact behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "0.1"
MAX_EVIDENCE_BYTES = 2_000_000
MAX_TIMEOUT_SECONDS = 600
EXPECTED_MANIFEST_KEYS = {
    "schema_version",
    "name",
    "documents",
    "implementation",
    "commands",
}
EXPECTED_COMMAND_KEYS = {"label", "argv", "timeout_seconds"}


class EvidenceCollectionError(RuntimeError):
    """Raised when a review bundle cannot be complete and trustworthy."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCollectionError(f"{label} must be a non-empty string")
    return value


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceCollectionError(f"cannot read manifest {path}: {exc}") from exc
    if not raw.strip():
        raise EvidenceCollectionError(f"manifest is empty: {path}")
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceCollectionError(f"manifest is not valid UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise EvidenceCollectionError("manifest root must be an object")
    unexpected = set(manifest) - EXPECTED_MANIFEST_KEYS
    missing = EXPECTED_MANIFEST_KEYS - set(manifest)
    if unexpected or missing:
        raise EvidenceCollectionError(
            f"manifest keys mismatch; missing={sorted(missing)} unexpected={sorted(unexpected)}"
        )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise EvidenceCollectionError(
            f"unsupported manifest schema_version: {manifest['schema_version']!r}"
        )
    _require_nonempty_string(manifest["name"], "manifest name")
    for category in ("documents", "implementation"):
        entries = manifest[category]
        if not isinstance(entries, list) or not entries:
            raise EvidenceCollectionError(f"{category} must contain at least one file")
        for index, entry in enumerate(entries):
            _require_nonempty_string(entry, f"{category}[{index}]")
    commands = manifest["commands"]
    if not isinstance(commands, list) or not commands:
        raise EvidenceCollectionError("commands must contain at least one command")
    labels: set[str] = set()
    for index, command in enumerate(commands):
        if not isinstance(command, dict):
            raise EvidenceCollectionError(f"commands[{index}] must be an object")
        unexpected = set(command) - EXPECTED_COMMAND_KEYS
        missing = {"label", "argv"} - set(command)
        if unexpected or missing:
            raise EvidenceCollectionError(
                f"commands[{index}] keys mismatch; missing={sorted(missing)} "
                f"unexpected={sorted(unexpected)}"
            )
        label = _require_nonempty_string(command["label"], f"commands[{index}].label")
        if label in labels:
            raise EvidenceCollectionError(f"duplicate command label: {label}")
        labels.add(label)
        argv = command["argv"]
        if not isinstance(argv, list) or not argv:
            raise EvidenceCollectionError(f"commands[{index}].argv must be non-empty")
        for arg_index, arg in enumerate(argv):
            _require_nonempty_string(arg, f"commands[{index}].argv[{arg_index}]")
        timeout = command.get("timeout_seconds", 300)
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
            raise EvidenceCollectionError(
                f"commands[{index}].timeout_seconds must be an integer from 1 to "
                f"{MAX_TIMEOUT_SECONDS}"
            )
    return manifest, raw


def _resolve_input(root: Path, relative: str, category: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvidenceCollectionError(f"{category} path escapes repository root: {relative}") from exc
    if not candidate.is_file():
        raise EvidenceCollectionError(f"{category} file does not exist: {relative}")
    return candidate


def _read_evidence_file(root: Path, relative: str, category: str) -> dict[str, Any]:
    path = _resolve_input(root, relative, category)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceCollectionError(f"cannot read {category} file {relative}: {exc}") from exc
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise EvidenceCollectionError(
            f"{category} file exceeds {MAX_EVIDENCE_BYTES} bytes: {relative}"
        )
    if not raw.strip():
        raise EvidenceCollectionError(f"{category} evidence is empty: {relative}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceCollectionError(f"{category} evidence is not UTF-8: {relative}") from exc
    return {
        "path": relative,
        "sha256": _sha256(raw),
        "bytes": len(raw),
        "text": text,
    }


def _run_command(root: Path, command: dict[str, Any]) -> dict[str, Any]:
    argv = [
        sys.executable if argument == "{python}" else argument
        for argument in command["argv"]
    ]
    timeout = command.get("timeout_seconds", 300)
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise EvidenceCollectionError(
            f"command executable not found for {command['label']}: {argv[0]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise EvidenceCollectionError(
            f"command timed out after {timeout}s: {command['label']}"
        ) from exc
    stdout = completed.stdout
    stderr = completed.stderr
    if completed.returncode != 0:
        detail = (stdout + stderr)[-2000:].decode("utf-8", errors="replace").strip()
        raise EvidenceCollectionError(
            f"command failed ({completed.returncode}): {command['label']}"
            + (f": {detail}" if detail else "")
        )
    combined = stdout + stderr
    if len(combined) > MAX_EVIDENCE_BYTES:
        raise EvidenceCollectionError(
            f"command evidence exceeds {MAX_EVIDENCE_BYTES} bytes: {command['label']}"
        )
    if not combined.strip():
        raise EvidenceCollectionError(f"command evidence is empty: {command['label']}")
    try:
        stdout_text = stdout.decode("utf-8")
        stderr_text = stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceCollectionError(
            f"command evidence is not UTF-8: {command['label']}"
        ) from exc
    return {
        "label": command["label"],
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "sha256": _sha256(combined),
        "bytes": len(combined),
    }


def _fence(text: str) -> str:
    longest = 0
    current = 0
    for character in text:
        if character == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "`" * max(4, longest + 1)


def _render_file_section(title: str, entries: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    for entry in entries:
        fence = _fence(entry["text"])
        lines.extend([
            f"### `{entry['path']}`",
            "",
            f"- sha256: `{entry['sha256']}`",
            f"- bytes: `{entry['bytes']}`",
            "",
            f"{fence}text",
            entry["text"].rstrip("\n"),
            fence,
            "",
        ])
    return lines


def _render_bundle(
    *,
    manifest: dict[str, Any],
    manifest_raw: bytes,
    documents: list[dict[str, Any]],
    implementation: list[dict[str, Any]],
    commands: list[dict[str, Any]],
) -> str:
    lines = [
        f"# Review evidence: {manifest['name']}",
        "",
        f"- schema_version: `{SCHEMA_VERSION}`",
        f"- manifest_sha256: `{_sha256(manifest_raw)}`",
        "- generated_at: `"
        + datetime.now(timezone.utc).isoformat(timespec="seconds")
        + "`",
        "- collection_mode: `argv-only; shell=false; fail-closed`",
        "",
    ]
    lines.extend(_render_file_section("Document evidence", documents))
    lines.extend(_render_file_section("Implementation evidence", implementation))
    lines.extend(["## Command evidence", ""])
    for command in commands:
        payload = ""
        if command["stdout"]:
            payload += "[stdout]\n" + command["stdout"]
        if command["stderr"]:
            payload += "[stderr]\n" + command["stderr"]
        fence = _fence(payload)
        lines.extend([
            f"### {command['label']}",
            "",
            f"- argv: `{json.dumps(command['argv'], ensure_ascii=False)}`",
            f"- returncode: `{command['returncode']}`",
            f"- sha256: `{command['sha256']}`",
            f"- bytes: `{command['bytes']}`",
            "",
            f"{fence}text",
            payload.rstrip("\n"),
            fence,
            "",
        ])
    rendered = "\n".join(lines).rstrip() + "\n"
    if not rendered.strip():
        raise EvidenceCollectionError("rendered review evidence is empty")
    return rendered


def _atomic_write(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def collect(*, root: Path, manifest_path: Path, output: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise EvidenceCollectionError(f"repository root does not exist: {root}")
    output = output.resolve()
    manifest, manifest_raw = _load_manifest(manifest_path.resolve())
    selected_inputs = {
        _resolve_input(root, relative, category).resolve()
        for category, entries in (
            ("document", manifest["documents"]),
            ("implementation", manifest["implementation"]),
        )
        for relative in entries
    }
    if output in selected_inputs:
        raise EvidenceCollectionError("output path cannot overwrite selected evidence")
    if output.exists():
        raise EvidenceCollectionError(
            f"output already exists; choose a fresh path to avoid stale evidence: {output}"
        )
    documents = [
        _read_evidence_file(root, relative, "document")
        for relative in manifest["documents"]
    ]
    implementation = [
        _read_evidence_file(root, relative, "implementation")
        for relative in manifest["implementation"]
    ]
    commands = [_run_command(root, command) for command in manifest["commands"]]
    content = _render_bundle(
        manifest=manifest,
        manifest_raw=manifest_raw,
        documents=documents,
        implementation=implementation,
        commands=commands,
    )
    _atomic_write(output, content)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "collected",
        "name": manifest["name"],
        "output": str(output),
        "output_sha256": _sha256(content.encode("utf-8")),
        "document_count": len(documents),
        "implementation_count": len(implementation),
        "command_count": len(commands),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root used to resolve manifest file entries",
    )
    args = parser.parse_args()
    try:
        result = collect(root=args.root, manifest_path=args.manifest, output=args.output)
    except EvidenceCollectionError as exc:
        print(f"review evidence collection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
