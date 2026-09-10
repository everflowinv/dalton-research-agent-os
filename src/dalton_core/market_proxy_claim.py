"""Derive explicitly mapped market-proxy evidence from governed price series."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .claim_index_authority import ClaimIndexAuthority, MARKET_PROXY
from .market_price import MarketPriceSeriesAuthority, provisional_bar_date
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
ACTOR_REF = "automation:market-proxy-claim"
_SCHEMA_PATH = Path(__file__).with_name("market_proxy_claim_schema.sql")
_FIELDS = {
    "mapping_ref", "source_series_company_ref", "source_ticker",
    "target_subject_ref", "metric_or_aspect", "aspect", "proxy_gap",
}


class MarketProxyClaimError(RuntimeError):
    pass


def validate_mapping(value: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise MarketProxyClaimError("proxy mapping has an invalid closed shape")
    wire = {}
    for key in _FIELDS:
        item = value[key]
        if not isinstance(item, str) or not item.strip():
            raise MarketProxyClaimError(f"proxy mapping {key} must be non-empty text")
        wire[key] = item.strip()
    if len(wire["proxy_gap"]) > 400:
        raise MarketProxyClaimError("proxy_gap exceeds 400 characters")
    return wire


def load_mappings(path: str | Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketProxyClaimError(f"market proxy config is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "mappings"} \
            or raw["schema_version"] != SCHEMA_VERSION or not isinstance(raw["mappings"], list):
        raise MarketProxyClaimError("market proxy config must be a 0.1 mappings object")
    mappings = [validate_mapping(item) for item in raw["mappings"]]
    refs = [item["mapping_ref"] for item in mappings]
    if len(refs) != len(set(refs)):
        raise MarketProxyClaimError("market proxy mapping_ref values must be unique")
    return mappings


class MarketProxyClaimAuthority:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def refresh(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        spec = validate_mapping(mapping)
        source = MarketPriceSeriesAuthority(self.store).latest_version(
            spec["source_series_company_ref"])
        if source is None:
            return {"status": "waiting_for_series", "mapping_ref": spec["mapping_ref"]}
        if source["ticker"] != spec["source_ticker"]:
            raise MarketProxyClaimError("mapped ticker differs from governed series ticker")
        if provisional_bar_date(source) is not None:
            return {"status": "waiting_for_settlement", "mapping_ref": spec["mapping_ref"]}
        existing = self.connection.execute(
            "SELECT record_json FROM market_proxy_claim_versions "
            "WHERE mapping_ref=? AND source_series_version_ref=?",
            (spec["mapping_ref"], source["id"]),).fetchone()
        if existing is not None:
            return {"status": "duplicate", **json.loads(existing["record_json"])}

        bar = source["bars"][-1]
        created_at = source["created_at"]
        identity = {"mapping_ref": spec["mapping_ref"],
                    "source_series_version_ref": source["id"],
                    "source_series_version_hash": source["content_hash"]}
        digest = content_hash(identity)
        invocation_ref = "market-proxy-invocation:" + digest[:32]
        if self.connection.execute(
                "SELECT 1 FROM model_invocations WHERE invocation_id=?",
                (invocation_ref,)).fetchone() is None:
            self.store.register_invocation({
                "schema_version": "0.1", "id": invocation_ref,
                "created_at": created_at, "work_order_ref": "work:market-proxy",
                "profile_ref": "profile:deterministic-market-proxy", "granularity": "task",
                "capability": "research", "provider": "deterministic",
                "model": "market-proxy-derivation-v1", "model_family": "deterministic",
                "runtime_ref": "runtime:dalton-core", "actor_ref": ACTOR_REF,
                "usage": {"tokens": 0}, "input_refs": [source["id"]],
                "output_refs": [], "started_at": created_at, "completed_at": created_at,
                "side_effects": [], "parent_ref": None,
            })
        evidence = self.store.register_evidence({
            "evidence_ref": "evidence:market-proxy:" + spec["mapping_ref"],
            "source_type": "public_web", "source_ref": source["id"],
            "retrieved_at": created_at,
            "source_lineage": [source["source_ref"], source["id"], source["content_hash"]],
            "independence_group": source["source_ref"], "actor_ref": ACTOR_REF,
        })
        claim_ref = "claim:market-proxy:" + spec["mapping_ref"]
        statement = (f"{spec['metric_or_aspect']} proxy {bar['adj_close']} {source['currency']} "
                     f"from {source['ticker']} on {bar['date']}; proxy gap: {spec['proxy_gap']}")
        claim = self.store.register_claim({
            "claim_ref": claim_ref, "subject_ref": spec["target_subject_ref"],
            "metric_or_aspect": spec["metric_or_aspect"], "period": bar["date"],
            "basis": "market_proxy", "normalized_statement": statement,
            # Qualitative prevents every legacy numeric/actual consumer from
            # admitting the proxy as a company-reported figure.
            "claim_kind": "qualitative", "value": None, "unit": None,
            "producer_invocation_refs": [invocation_ref], "actor_ref": ACTOR_REF,
        })
        self.store.relate_evidence({
            "id": "relation:market-proxy:" + digest[:32],
            "evidence_version_ref": evidence["evidence_version_id"],
            "claim_version_ref": claim["claim_version_id"], "relation": "supports",
        })
        indexed = ClaimIndexAuthority(self.store).record_entry(
            claim_version_ref=claim["claim_version_id"], claim_version_hash=claim["content_hash"],
            claim_ref=claim_ref, claim_created_at=created_at,
            subject_ref=spec["target_subject_ref"], metric_or_aspect=spec["metric_or_aspect"],
            period_key=bar["date"], claim_kind="qualitative", aspect=spec["aspect"],
            aspect_source="rule", as_of=bar["date"], as_of_basis="period_end",
            importance="other", importance_basis="governed yfinance proxy series",
            dedupe_group_key=f"market-proxy|{spec['mapping_ref']}|{bar['date']}",
            tagger_ref="tagger:market-proxy:v1", tagger_hash=content_hash(spec),
            actor_ref=ACTOR_REF, created_at=created_at, evidence_kind=MARKET_PROXY)
        record = {"schema_version": SCHEMA_VERSION,
                  "id": "market-proxy-claim-version:" + digest[:32],
                  "created_at": created_at, **spec,
                  "source_series_version_ref": source["id"],
                  "source_series_version_hash": source["content_hash"],
                  "source_ref": source["source_ref"], "as_of": bar["date"],
                  "value": bar["adj_close"], "unit": source["currency"],
                  "claim_version_ref": claim["claim_version_id"],
                  "claim_index_entry_ref": indexed["id"]}
        record["content_hash"] = content_hash(record)
        with self.store._transaction() as cur:
            cur.execute("INSERT INTO market_proxy_claim_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                record["id"], spec["mapping_ref"], spec["target_subject_ref"],
                spec["source_series_company_ref"], source["id"], source["content_hash"],
                claim["claim_version_id"], spec["proxy_gap"], canonical_json(record),
                record["content_hash"], created_at))
        return {"status": "fresh", **record}

    def refresh_all(self, mappings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        results = [self.refresh(item) for item in mappings]
        return {"status": "idle" if not mappings else "settled", "results": results}

    def for_subject(self, subject_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json FROM market_proxy_claim_versions WHERE target_subject_ref=? "
            "ORDER BY created_at, version_id", (subject_ref,)).fetchall()
        return [json.loads(row["record_json"]) for row in rows]


__all__ = ["MarketProxyClaimAuthority", "MarketProxyClaimError", "load_mappings",
           "validate_mapping"]
