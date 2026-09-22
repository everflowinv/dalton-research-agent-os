#!/usr/bin/env python3
"""Renew the two audited Google provider-control rate cards, dry-run first.

This script never changes a rate, mode, model, or thinking level.  It only
updates ``verifiedAt`` and ``expiresAt`` after an operator has rechecked the
listed primary pricing sources.  ``--apply`` additionally requires the exact
SHA-256 printed by a dry run, so an intervening host-config edit is refused.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from dalton_core.openclaw_catalog_reconcile import load_openclaw_config


MODE = "google-generative-ai-count-tokens-v1"
SOURCES = (
    "https://ai.google.dev/gemini-api/docs/pricing",
    "https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing",
)
EXPECTED: dict[str, dict[str, Any]] = {
    "profile:gemini-3-8-flash": {
        "model": "google/gemini-3.8-flash",
        "thinkingLevel": "low",
        "rates": {
            "inputUsdPerMillion": "1.50",
            "cachedInputUsdPerMillion": "1.50",
            "cacheWriteUsdPerMillion": "1.50",
            "outputUsdPerMillion": "7.50",
        },
    },
    "profile:gemini-3-1-pro-preview": {
        "model": "google/gemini-3.1-pro-preview",
        "rates": {
            "inputUsdPerMillion": "4.00",
            "cachedInputUsdPerMillion": "4.00",
            "cacheWriteUsdPerMillion": "4.00",
            "outputUsdPerMillion": "18.00",
        },
    },
}


class RenewalError(ValueError):
    """The host config or requested renewal is outside the audited boundary."""


def _time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise RenewalError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RenewalError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _profiles(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        profiles = config["plugins"]["entries"][
            "dalton-openclaw-model-broker"
        ]["config"]["profiles"]
    except (KeyError, TypeError) as exc:
        raise RenewalError("OpenClaw broker profiles are unavailable") from exc
    if not isinstance(profiles, list) or not all(
        isinstance(profile, dict) for profile in profiles
    ):
        raise RenewalError("OpenClaw broker profiles must be an array of objects")
    return profiles


def renewed_config(
    config: Mapping[str, Any], *, verified_at: datetime, expires_at: datetime
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate the audited shape and return a copy with only dates changed."""

    if verified_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise RenewalError("verified_at cannot be in the future")
    horizon = expires_at - verified_at
    if horizon <= timedelta(0) or horizon > timedelta(days=31):
        raise RenewalError("expires_at must be after verified_at and at most 31 days later")
    if expires_at <= datetime.now(timezone.utc):
        raise RenewalError("expires_at must still be in the future")
    wire = copy.deepcopy(dict(config))
    profiles = _profiles(wire)
    controlled_ids = [
        str(profile.get("id")) for profile in profiles
        if "providerControls" in profile
    ]
    if len(controlled_ids) != len(set(controlled_ids)):
        raise RenewalError("providerControls profile ids must be unique")
    controlled = {
        str(profile.get("id")): profile
        for profile in profiles
        if "providerControls" in profile
    }
    if set(controlled) != set(EXPECTED):
        raise RenewalError(
            "providerControls must exist on exactly the two audited profiles: "
            + ", ".join(sorted(EXPECTED))
        )
    changes: list[dict[str, str]] = []
    for profile_id, expected in EXPECTED.items():
        profile = controlled[profile_id]
        if profile.get("model") != expected["model"]:
            raise RenewalError(f"{profile_id} model route has changed")
        controls = profile.get("providerControls")
        if not isinstance(controls, dict) or controls.get("mode") != MODE:
            raise RenewalError(f"{profile_id} provider-control mode has changed")
        allowed_control_keys = {"mode", "rateCard"} | (
            {"thinkingLevel"} if "thinkingLevel" in expected else set()
        )
        if set(controls) != allowed_control_keys:
            raise RenewalError(f"{profile_id} provider-control fields have changed")
        if controls.get("thinkingLevel") != expected.get("thinkingLevel"):
            raise RenewalError(f"{profile_id} thinking level has changed")
        rate = controls.get("rateCard")
        if not isinstance(rate, dict) or set(rate) != {
            "model", "serviceTier", *expected["rates"], "verifiedAt", "expiresAt"
        }:
            raise RenewalError(f"{profile_id} rate-card fields have changed")
        if rate.get("model") != expected["model"] or rate.get("serviceTier") != "default":
            raise RenewalError(f"{profile_id} rate-card route has changed")
        for name, value in expected["rates"].items():
            if rate.get(name) != value:
                raise RenewalError(
                    f"{profile_id} {name} is {rate.get(name)!r}, expected {value!r}; "
                    "re-audit and update this script rather than silently changing price"
                )
        changes.append({
            "profile_id": profile_id,
            "old_verified_at": str(rate["verifiedAt"]),
            "old_expires_at": str(rate["expiresAt"]),
            "verified_at": _wire(verified_at),
            "expires_at": _wire(expires_at),
        })
        rate["verifiedAt"] = _wire(verified_at)
        rate["expiresAt"] = _wire(expires_at)
    return wire, changes


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--openclaw-config", type=Path, required=True)
    parser.add_argument("--verified-at", required=True)
    parser.add_argument("--expires-at", required=True)
    parser.add_argument("--expected-config-sha256")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    target = args.openclaw_config.expanduser().resolve()
    raw = target.read_bytes()
    current_hash = _sha256(raw)
    if args.apply and not args.expected_config_sha256:
        raise RenewalError("--apply requires --expected-config-sha256 from a dry run")
    if args.expected_config_sha256 and args.expected_config_sha256 != current_hash:
        raise RenewalError("OpenClaw config changed since the reviewed dry run")
    config = load_openclaw_config(target)
    updated, changes = renewed_config(
        config,
        verified_at=_time(args.verified_at, "verified_at"),
        expires_at=_time(args.expires_at, "expires_at"),
    )
    output: dict[str, Any] = {
        "status": "dry_run",
        "config_path": str(target),
        "current_config_sha256": current_hash,
        "changes": changes,
        "unchanged_fields": ["mode", "model", "thinkingLevel", "rateCard prices"],
        "evidence_sources_to_recheck": list(SOURCES),
    }
    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = target.with_name(f"{target.name}.bak-provider-controls-{stamp}")
        if backup.exists():
            raise RenewalError(f"backup already exists: {backup}")
        backup.write_bytes(raw)
        os.chmod(backup, target.stat().st_mode & 0o777)
        rendered = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, target.stat().st_mode & 0o777)
            if _sha256(target.read_bytes()) != current_hash:
                raise RenewalError(
                    "OpenClaw config changed while the renewal was prepared"
                )
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        # Parse and validate the written file before reporting success.  The
        # backup remains beside it for a one-file rollback.
        renewed_config(
            load_openclaw_config(target),
            verified_at=_time(args.verified_at, "verified_at"),
            expires_at=_time(args.expires_at, "expires_at"),
        )
        output.update({
            "status": "applied",
            "backup_path": str(backup),
            "new_config_sha256": _sha256(target.read_bytes()),
        })
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
