#!/usr/bin/env python3
"""Export registered connection metadata from explicit read-only authorities.

The result is an inventory, not a connectivity or authorization claim.
"""
from __future__ import annotations
import argparse, json, os, sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet
from dalton_core.store import content_hash


def broker_profile_ids(config_path: Path) -> frozenset[str]:
    """Read the broker config and project the profile ids it currently discovers."""
    from dalton_core.openclaw_catalog_reconcile import openclaw_broker_profiles_from_config
    raw = json.loads(config_path.resolve().read_text(encoding="utf-8"))
    profiles = openclaw_broker_profiles_from_config(raw, checked_at=datetime.now(timezone.utc))
    return frozenset(str(profile["id"]) for profile in profiles)


def export_catalog(model_db: Path, core_db: Path, broker_socket: Path, output: Path,
                   discoverable_model_ids: AbstractSet[str] | None = None) -> dict:
    models, sources = [], []
    with closing(sqlite3.connect(model_db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for (raw,) in connection.execute("""SELECT p.profile_json FROM model_endpoint_profile_versions p
                WHERE p.version=(SELECT MAX(q.version) FROM model_endpoint_profile_versions q
                                 WHERE q.profile_id=p.profile_id) ORDER BY p.profile_id"""):
            record = json.loads(raw)
            if record.get("status") == "retired":
                continue
            if discoverable_model_ids is not None and record.get("id") not in discoverable_model_ids:
                continue
            item = {key: record[key] for key in ("id", "provider", "model", "family", "adapter_ref",
                                                 "credential_slot_ref", "capabilities", "modalities")}
            item["transport"] = dict(kind="broker", endpoint_ref="broker:openclaw-model",
                                     socket_path=str(broker_socket.resolve()), config_path=None)
            models.append(item)
    with closing(sqlite3.connect(core_db.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for (raw,) in connection.execute("""SELECT p.record_json FROM connector_profile_versions p
                WHERE p.version_number=(SELECT MAX(q.version_number) FROM connector_profile_versions q
                                        WHERE q.connector_ref=p.connector_ref)
                ORDER BY p.connector_ref"""):
            record = json.loads(raw)
            item = {key: record[key] for key in ("id", "connector_ref", "capability_id", "auth_mode",
                                                 "credential_slot_refs", "allowed_operations", "allowed_hosts")}
            item["transport"] = dict(kind="connector", endpoint_ref=record["adapter_ref"],
                                     socket_path=None, config_path=None)
            sources.append(item)
    body = dict(schema_version="dalton-shared-connection-catalog-0.1", models=models, sources=sources)
    value = {**body, "content_hash": content_hash(body)}
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if output.exists():
        if json.loads(output.read_text()) != value:
            raise ValueError("Existing catalog differs; export to a new versioned path")
    else:
        fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
    return dict(status="exported_registered_connection_metadata", models=len(models), sources=len(sources),
                catalog_hash=value["content_hash"], broker_catalog_filtered=discoverable_model_ids is not None,
                connectivity_verified=False, research_copied=False, approvals_copied=False,
                credential_values_copied=False, provider_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-db", type=Path, required=True); parser.add_argument("--core-db", type=Path, required=True)
    parser.add_argument("--broker-socket", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broker-config", type=Path,
                        help="optional read-only OpenClaw config; export only models its broker catalog discovers")
    args = parser.parse_args()
    allowed = None if args.broker_config is None else broker_profile_ids(args.broker_config)
    print(json.dumps(export_catalog(args.model_db, args.core_db, args.broker_socket, args.output, allowed)))


if __name__ == "__main__": main()
