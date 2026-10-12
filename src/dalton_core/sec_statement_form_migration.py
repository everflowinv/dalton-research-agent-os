"""Explicit compatible SEC v4 -> v5 approval migration, never a human act.

The source and permissions remain those already approved. Only the closed
filing-form output contract gains exact amended forms; the prior record and
enabled shared-source snapshot are retained in the new approval's proof.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import content_hash

ACTOR = "automation:sec-statements-form-contract-migration"
SCHEMA = "sec-statements-form-contract-migration:0.1"
REASON = "Preserve exact SEC amended filing identities under the existing read-only source approval"


def validate_migration(wire):
    from .connector_governance import ConnectorGovernance, GOVERNANCE_FIELDS
    from .sec_financials_core import sec_financials_identity
    proof = wire.get("compatibility_migration")
    if (set(wire) != set(GOVERNANCE_FIELDS) | {"compatibility_migration"}
            or not isinstance(proof, dict) or set(proof) != {
                "schema_version", "actor_ref", "reason", "prior_record", "source_binding"}
            or proof["schema_version"] != SCHEMA or proof["actor_ref"] != ACTOR
            or proof["reason"] != REASON or wire["approved_by"] != ACTOR
            or wire["id"] != "connector-governance:sec-financial-statements:v5"):
        raise ValueError("SEC form migration shape or actor differs")
    prior = ConnectorGovernance(proof["prior_record"])
    old = sec_financials_identity(version=4); new = sec_financials_identity(version=5)
    if (not prior.approved or prior.id != "connector-governance:sec-financial-statements:v4"
            or prior.wire["expected_source_hash"] != old["source_hash"]
            or prior.wire["expected_schema_hash"] != old["schema_hash"]
            or wire["expected_source_hash"] != new["source_hash"]
            or wire["expected_schema_hash"] != new["schema_hash"]):
        raise ValueError("SEC form migration lacks exact approved predecessor")
    start = ConnectorGovernance._parse_time(wire["effective_from"], "effective_from")
    if (start < ConnectorGovernance._parse_time(prior.effective_from, "prior effective_from")
            or (prior.wire["effective_until"] is not None and
                start >= ConnectorGovernance._parse_time(prior.wire["effective_until"], "prior effective_until"))):
        raise ValueError("SEC form migration is outside the original approval window")
    expected = dict(prior.wire)
    for field in ("id", "policy_ref", "approval_ref", "decision_ref", "registry_revision_ref", "attestation_ref"):
        expected[field] = expected[field].rsplit(":v", 1)[0] + ":v5" if field != "registry_revision_ref" else old["capability_id"] + "@v5"
    expected.update(approved_by=ACTOR, effective_from=wire["effective_from"],
                    expected_schema_hash=new["schema_hash"], compatibility_migration=proof)
    expected.pop("content_hash")
    if content_hash(expected) != wire["content_hash"] or {**expected, "content_hash": wire["content_hash"]} != wire:
        raise ValueError("SEC form migration changes source, permissions or approval scope")
    binding = proof["source_binding"]
    if not isinstance(binding, dict) or set(binding) != {"workspace_ref", "workspace_hash", "catalog_hash", "source"}:
        raise ValueError("SEC form migration source binding is incomplete")
    source = binding["source"]
    if (not isinstance(source, dict) or source.get("enabled", True) is not True
            or source.get("capability_id") != "capability:dalton:connector:sec-edgar"
            or source.get("connector_ref") != "connector:sec-edgar" or source.get("auth_mode") != "none"
            or source.get("allowed_hosts") != ["data.sec.gov", "www.sec.gov"]
            or set(source.get("allowed_operations", [])) != {"list_filings", "get_company_facts"}
            or source.get("transport") != {"config_path": None, "endpoint_ref": "transport:public-http:0.1",
                                           "kind": "connector", "socket_path": None}
            or source.get("credential_slot_refs") != []
            or any(not isinstance(binding[k], str) or len(binding[k]) != 64
                   for k in ("workspace_hash", "catalog_hash"))):
        raise ValueError("SEC form migration source endpoint or permissions changed")


def preview(workspace):
    from .connector_governance import ConnectorGovernance
    from .workspace_runtime_setup import _catalog
    directory = workspace.state_dir / "connector-governance"
    if workspace.lifecycle != "active":
        return {"status": "skipped", "reason": "workspace is not active"}
    old_path = directory / "sec-financial-statements-v4.json"
    if not old_path.is_file() or old_path.is_symlink():
        return {"status": "skipped", "reason": "no existing regular v4 approval"}
    prior = ConnectorGovernance.load(old_path)
    if not prior.approved:
        return {"status": "skipped", "reason": "existing source is not approved"}
    now = datetime.now(timezone.utc)
    if (now < prior._parse_time(prior.effective_from, "effective_from")
            or (prior.wire["effective_until"] is not None and
                now >= prior._parse_time(prior.wire["effective_until"], "effective_until"))):
        return {"status": "skipped", "reason": "existing approval is outside its effective window"}
    catalog = getattr(workspace, "migration_catalog", None) or _catalog(workspace)
    sources = [] if catalog is None else [s for s in catalog["sources"] if s.get("connector_ref") == "connector:sec-edgar"]
    if not sources:
        return {"status": "skipped", "reason": "SEC source is disabled or not selected"}
    if any(s.get("enabled", True) is not True for s in sources):
        return {"status": "skipped", "reason": "SEC source is explicitly disabled"}
    if len(sources) != 1:
        raise ValueError("SEC form migration requires one exact enabled source")
    proof = {"schema_version": SCHEMA, "actor_ref": ACTOR, "reason": REASON,
             "prior_record": prior.wire,
             "source_binding": {"workspace_ref": workspace.workspace_id,
                 "workspace_hash": workspace.content_hash, "catalog_hash": catalog["content_hash"],
                 "source": sources[0]}}
    from .sec_financials_core import sec_financials_identity
    new = dict(prior.wire)
    for field in ("id", "policy_ref", "approval_ref", "decision_ref", "registry_revision_ref", "attestation_ref"):
        new[field] = new[field].rsplit(":v", 1)[0] + ":v5" if field != "registry_revision_ref" else prior.capability_id + "@v5"
    new.update(approved_by=ACTOR, effective_from=datetime.now(timezone.utc).isoformat(),
               expected_schema_hash=sec_financials_identity(version=5)["schema_hash"], compatibility_migration=proof)
    new.pop("content_hash"); new["content_hash"] = content_hash(new)
    validate_migration(new)
    target = directory / "sec-financial-statements-v5.json"
    if target.exists():
        existing = ConnectorGovernance.load(target)
        if "compatibility_migration" not in existing.wire:
            from .sec_financials_cli import _load_governance
            _load_governance(target)
            return {"status": "skipped", "reason": "existing separately approved v5 source is preserved", "path": str(target)}
        validate_migration(existing.wire)
        if (existing.wire["compatibility_migration"]["prior_record"] != prior.wire
                or any(existing.wire["compatibility_migration"]["source_binding"][k] != proof["source_binding"][k]
                       for k in ("workspace_ref", "catalog_hash", "source"))):
            raise ValueError("SEC form migration existing predecessor differs")
        return {"status": "existing", "path": str(target), "record": existing.wire}
    return {"status": "eligible", "path": str(target), "record": new}


def apply(workspace):
    from .workspace_runtime_setup import _atomic_seed
    proposed = preview(workspace)
    if proposed["status"] == "eligible":
        _atomic_seed(Path(proposed["path"]), proposed["record"])
        from .connector_governance import ConnectorGovernance
        saved = ConnectorGovernance.load(proposed["path"])
        if saved.wire != proposed["record"]:
            raise ValueError("SEC form migration raced a different approval")
        return {**proposed, "status": "applied"}
    return proposed


def managed_workspace(manager_path, environment):
    """Read existing manager registry only; legacy is not a fabricated workspace."""
    from types import SimpleNamespace
    from .workspace_manager import _config
    from .workspace import load_workspace_manifest
    manager_path = Path(manager_path).expanduser().resolve()
    config = _config(manager_path)
    if environment != "legacy":
        workspace = load_workspace_manifest(Path(config["host_root"]) / "workspaces" / environment / "workspace.json")
        if environment not in {workspace.workspace_id, workspace.workspace_root.name}:
            raise ValueError("SEC migration manager environment identity differs")
        return workspace
    legacy = config.get("legacy_workspace")
    if not legacy:
        raise ValueError("SEC migration has no registered legacy environment")
    state = Path(legacy["state_dir"]).expanduser().resolve()
    service_path = Path(legacy["config_path"]).expanduser().resolve()
    service_bytes = service_path.read_bytes(); service = json.loads(service_bytes)
    if Path(service["core_db"]).expanduser().resolve() != state / "core.sqlite":
        raise ValueError("SEC migration legacy service and registry state differ")
    catalog_path = Path(config["connections_path"]).expanduser().resolve()
    if catalog_path not in [Path(p).expanduser().resolve() for p in config["shared_readonly_paths"]]:
        raise ValueError("SEC migration legacy source catalog is not declared")
    catalog = json.loads(catalog_path.read_bytes())
    if (catalog.get("schema_version") != "dalton-shared-connection-catalog-0.1"
            or catalog.get("content_hash") != content_hash({k:v for k,v in catalog.items() if k != "content_hash"})):
        raise ValueError("SEC migration legacy source catalog hash differs")
    import hashlib
    binding = {"manager_sha256": hashlib.sha256(manager_path.read_bytes()).hexdigest(),
               "service_sha256": hashlib.sha256(service_bytes).hexdigest(), "legacy": legacy}
    return SimpleNamespace(workspace_id="legacy", lifecycle=legacy.get("lifecycle", "active"),
        state_dir=state, workspace_root=service_path.parent.parent,
        content_hash=content_hash(binding), migration_catalog=catalog)


def main(argv=None):
    from .workspace import load_workspace_manifest
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--workspace")
    source.add_argument("--manager")
    parser.add_argument("--environment")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--sync-writer", action="store_true", help="Install only the approved writer governance flag; never restart")
    parser.add_argument("--launch-agents-dir", default=str(Path.home() / "Library" / "LaunchAgents"))
    parser.add_argument("--expected-plist-sha256")
    args = parser.parse_args(argv)
    if bool(args.manager) != bool(args.environment):
        parser.error("--manager requires an exact --environment")
    workspace = managed_workspace(args.manager, args.environment) if args.manager else load_workspace_manifest(args.workspace)
    if args.sync_writer:
        from .sec_statement_writer_contract_sync import preview as writer_preview, apply as writer_apply
        if args.apply and not args.expected_plist_sha256:
            parser.error("writer sync apply requires --expected-plist-sha256")
        result = (writer_apply(workspace, args.launch_agents_dir, expected_plist_sha256=args.expected_plist_sha256)
                  if args.apply else writer_preview(workspace, args.launch_agents_dir))
        result = {k:v for k,v in result.items() if k != "plist"}
    else:
        if args.expected_plist_sha256:
            parser.error("--expected-plist-sha256 requires --sync-writer")
        result = apply(workspace) if args.apply else preview(workspace)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
