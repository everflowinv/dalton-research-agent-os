#!/usr/bin/env python3
"""Run one isolated, credential-free SEC quarterly revenue growth canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dalton_core.sec_public_adapter import (  # noqa: E402
    DEFAULT_USER_AGENT,
    SecCompanyConceptHttpAdapter,
)
from dalton_core.store import canonical_json, content_hash  # noqa: E402


class MemorySink:
    def __init__(self) -> None:
        self.parts: list[bytes] = []

    def write(self, value: bytes) -> None:
        self.parts.append(bytes(value))

    def bytes(self) -> bytes:
        return b"".join(self.parts)


def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cik", default="0000789019")
    parser.add_argument(
        "--concept",
        default="RevenueFromContractWithCustomerExcludingAssessedTax",
    )
    parser.add_argument("--filed-from", required=True)
    parser.add_argument("--filed-to", required=True)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    parameters = {
        "cik": args.cik,
        "taxonomy": "us-gaap",
        "concept": args.concept,
        "unit": "USD",
        "form": "10-Q",
        "filed_from": args.filed_from,
        "filed_to": args.filed_to,
    }
    request = {
        "parameters": parameters,
        "deadline_at": _wire_time(now + timedelta(seconds=30)),
        "allowed_hosts": ["data.sec.gov"],
        "network_policy": {"allow_redirects": False, "max_redirects": 0},
        "max_response_bytes": 5_000_000,
    }
    request["content_hash"] = content_hash(request)
    sink = MemorySink()
    observation = SecCompanyConceptHttpAdapter(
        user_agent=args.user_agent,
        clock=lambda: now,
    )(request, sink)
    if observation["outcome"] != "succeeded":
        print(canonical_json(observation))
        return 1
    normalized = observation["structured_output"]
    raw_sha256 = hashlib.sha256(sink.bytes()).hexdigest()
    direction = "up" if not normalized["growth_percent"].startswith("-") else "down"
    statement = (
        f"{normalized['entity_name']} reported {normalized['label']} of "
        f"{normalized['unit']} {normalized['current']['value']} for "
        f"{normalized['current']['start']}..{normalized['current']['end']}, "
        f"{direction} {normalized['growth_percent'].lstrip('-')}% year over year "
        f"from {normalized['unit']} {normalized['prior']['value']} in the "
        "comparable quarter."
    )
    result = {
        "schema_version": "0.1",
        "id": "sec-quarterly-growth-observation:" + content_hash({
            "request_hash": request["content_hash"],
            "normalized_hash": normalized["content_hash"],
        }),
        "created_at": _wire_time(now),
        "source_ref": "source:sec-edgar",
        "operation": "get_company_facts",
        "entity_name": normalized["entity_name"],
        "cik": normalized["cik"],
        "taxonomy": normalized["taxonomy"],
        "concept": normalized["concept"],
        "label": normalized["label"],
        "unit": normalized["unit"],
        "form": normalized["form"],
        "filed_from": normalized["filed_from"],
        "filed_to": normalized["filed_to"],
        "current": normalized["current"],
        "prior": normalized["prior"],
        "growth_percent": normalized["growth_percent"],
        "normalized_statement": statement,
        "source_record_refs": normalized["source_record_refs"],
        "provider_request_ref": observation["provider_request_id"],
        "raw_sha256": raw_sha256,
    }
    result["content_hash"] = content_hash(result)
    destination = output_dir / "result.json"
    destination.write_text(canonical_json(result) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
