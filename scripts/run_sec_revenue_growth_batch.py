#!/usr/bin/env python3
"""Run the Gate 1 five-issuer SEC revenue-growth replay bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dalton_core.store import content_hash  # noqa: E402


ISSUERS = (
    {"ticker": "MSFT", "name": "Microsoft", "cik": "789019"},
    {"ticker": "AAPL", "name": "Apple", "cik": "320193"},
    {"ticker": "NVDA", "name": "NVIDIA", "cik": "1045810"},
    {"ticker": "WMT", "name": "Walmart", "cik": "104169"},
    {"ticker": "AMZN", "name": "Amazon", "cik": "1018724"},
)
FAIL_CLOSED_SAMPLE = {
    "ticker": "WMT-STALE-SALESREVENUENET",
    "name": "Walmart stale concept control",
    "cik": "104169",
    "concept_candidates": ["SalesRevenueNet"],
}


class BatchVerificationError(RuntimeError):
    """A batch child did not satisfy the exact Gate 1 contract."""


def _wire_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BatchVerificationError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BatchVerificationError(f"JSON root must be an object: {path}")
    return value


def _padded_cik(cik: str) -> str:
    return cik.zfill(10)


def _filing_url(cik: str, accession: str) -> str:
    directory = accession.replace("-", "")
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{directory}/{accession}-index.html"
    )


def validate_success_result(
    result: dict[str, Any], *, ticker: str, cik: str
) -> dict[str, Any]:
    try:
        facts = result["candidate"]["source_facts"]
        verifications = result["candidate"]["verifications"]
        counts = result["formal_ledger_counts"]
        gates = result["human_gate_counts"]
        integrity = result["integrity"]
        closure = result["closure"]
        replay = result["closure_replay"]
        promotion = result["promotion"]
        model_counts = result["model_accounting_counts"]
    except (KeyError, TypeError) as exc:
        raise BatchVerificationError(
            f"{ticker} result is missing Gate 1 evidence: {exc}"
        ) from exc
    if result.get("status") != "autonomous-closed":
        raise BatchVerificationError(f"{ticker} did not close autonomously")
    parameters = result.get("plan", {}).get("parameters", {})
    if (
        result.get("plan", {}).get("operation") != "get_company_facts"
        or parameters.get("cik") != _padded_cik(cik)
        or facts.get("cik") != _padded_cik(cik)
    ):
        raise BatchVerificationError(f"{ticker} plan/CIK binding changed")
    expected_counts = {
        "evidence_versions": 1,
        "claim_versions": 1,
        "thesis_versions": 0,
    }
    if counts != expected_counts:
        raise BatchVerificationError(f"{ticker} Ledger cardinality changed: {counts}")
    if gates != {"plan_approvals": 0, "claim_reviews": 0}:
        raise BatchVerificationError(f"{ticker} unexpectedly used human gates: {gates}")
    if set(model_counts.values()) != {0}:
        raise BatchVerificationError(
            f"{ticker} unexpectedly used a model: {model_counts}"
        )
    if set(integrity.values()) != {"ok"}:
        raise BatchVerificationError(f"{ticker} integrity failed: {integrity}")
    if (
        closure.get("status") != "fresh"
        or replay.get("status") != "duplicate"
        or closure.get("answer_binding_ref") != replay.get("answer_binding_ref")
    ):
        raise BatchVerificationError(f"{ticker} closure replay did not converge")
    current = facts.get("current", {})
    prior = facts.get("prior", {})
    accession = facts.get("latest_accession")
    if (
        not isinstance(accession, str)
        or current.get("accession") != accession
        or prior.get("accession") != accession
    ):
        raise BatchVerificationError(f"{ticker} comparison is not same-filing")
    try:
        claim_growth = Decimal(result["candidate"].get("value"))
        source_growth = Decimal(facts.get("growth_percent"))
    except (InvalidOperation, TypeError) as exc:
        raise BatchVerificationError(f"{ticker} growth is not a Decimal") from exc
    if claim_growth != source_growth:
        raise BatchVerificationError(f"{ticker} Claim and source growth disagree")
    verifier_map = {item.get("kind"): item.get("verdict") for item in verifications}
    if verifier_map != {"source": "pass", "numeric": "pass"}:
        raise BatchVerificationError(f"{ticker} verifier status changed: {verifier_map}")
    claim_hash = promotion.get("claim_version_hash")
    if not isinstance(claim_hash, str) or len(claim_hash) != 64:
        raise BatchVerificationError(f"{ticker} formal Claim hash is missing")
    return facts


def summarize_success(
    result: dict[str, Any], *, issuer: dict[str, str], result_path: Path
) -> dict[str, Any]:
    facts = validate_success_result(
        result, ticker=issuer["ticker"], cik=issuer["cik"]
    )
    accession = facts["latest_accession"]
    authority_identity = {
        "plan_ref": result["plan"]["ref"],
        "plan_hash": result["plan"]["hash"],
        "artifact_refs": result["candidate"]["artifact_refs"],
        "source_facts_hash": facts["content_hash"],
        "formal_claim_ref": result["promotion"]["claim_version_ref"],
        "formal_claim_hash": result["promotion"]["claim_version_hash"],
        "answer_binding_ref": result["closure"]["answer_binding_ref"],
    }
    return {
        "ticker": issuer["ticker"],
        "name": issuer["name"],
        "cik": facts["cik"],
        "entity_name": facts["entity_name"],
        "accession": accession,
        "filing_url": _filing_url(facts["cik"], accession),
        "filed_from": facts["filed_from"],
        "filed_to": facts["filed_to"],
        "taxonomy": facts["taxonomy"],
        "concept": facts["concept"],
        "eligible_concepts": facts["eligible_concepts"],
        "label": facts["label"],
        "unit": facts["unit"],
        "current": facts["current"],
        "prior": facts["prior"],
        "growth_percent": facts["growth_percent"],
        "selection_basis": facts["selection_basis"],
        "source_record_refs": facts["source_record_refs"],
        "source_verifier": "pass",
        "numeric_verifier": "pass",
        "semantic_verification_status": result["candidate"][
            "semantic_verification_status"
        ],
        "thesis_impact": "not yet run",
        "actual_model_cost": {"currency": "USD", "amount": "0.00"},
        "plan_ref": result["plan"]["ref"],
        "plan_hash": result["plan"]["hash"],
        "formal_claim_ref": result["promotion"]["claim_version_ref"],
        "formal_claim_hash": result["promotion"]["claim_version_hash"],
        "answer_binding_ref": result["closure"]["answer_binding_ref"],
        "authority_hash": content_hash(authority_identity),
        "result_sha256": _sha256(result_path),
        "result_path": f"samples/{issuer['ticker']}/result.json",
        "replay_result_path": f"samples/{issuer['ticker']}/replay.json",
    }


def validate_fail_closed_result(
    result: dict[str, Any], *, expected_concepts: list[str]
) -> dict[str, Any]:
    if result.get("status") != "expected-fail-closed":
        raise BatchVerificationError("control sample did not fail closed as expected")
    parameters = result.get("plan", {}).get("parameters", {})
    if parameters.get("concept_candidates") != expected_concepts:
        raise BatchVerificationError("control sample concept allowlist changed")
    counts = result.get("formal_ledger_counts")
    if counts != {
        "evidence_versions": 0,
        "claim_versions": 0,
        "thesis_versions": 0,
    }:
        raise BatchVerificationError(
            f"control sample wrote formal Ledger rows: {counts}"
        )
    candidate_counts = result.get("candidate_counts", {})
    if candidate_counts.get("candidate_evidence_versions") != 0 or candidate_counts.get(
        "candidate_claim_versions"
    ) != 0:
        raise BatchVerificationError(
            f"control sample staged a candidate: {candidate_counts}"
        )
    if set(result.get("integrity", {}).values()) != {"ok"}:
        raise BatchVerificationError("control sample integrity failed")
    if result.get("human_gate_counts") != {
        "plan_approvals": 0,
        "claim_reviews": 0,
    }:
        raise BatchVerificationError("control sample unexpectedly used human gates")
    if set(result.get("model_accounting_counts", {}).values()) != {0}:
        raise BatchVerificationError("control sample unexpectedly used a model")
    return {
        "status": result["status"],
        "cik": parameters["cik"],
        "concept_candidates": parameters["concept_candidates"],
        "failure_status": result.get("failure", {}).get("status"),
        "formal_ledger_counts": counts,
        "candidate_counts": candidate_counts,
        "integrity": result["integrity"],
    }


def _money(value: str) -> str:
    return f"{int(value):,}"


def render_brief(batch: dict[str, Any]) -> str:
    lines = [
        "# SEC 五公司季度收入增速验证简报 v0",
        "",
        f"生成时间：{batch['generated_at']}",
        "",
        f"代码 commit：`{batch['repo_commit']}`",
        "",
        (
            f"结果：{len(batch['samples'])}/5 家公司完成同一条 policy-authorized "
            "SEC Company Facts → verifier → formal Claim → Backlog closure 主链；"
            "全部零逐 plan/逐 Claim 人工 gate。"
        ),
        "",
    ]
    for sample in batch["samples"]:
        lines.extend([
            f"## {sample['ticker']} — {sample['entity_name']}",
            "",
            f"- Filing：[{sample['accession']}]({sample['filing_url']})；CIK `{sample['cik']}`。",
            (
                f"- 期间与数值：{sample['current']['start']}..{sample['current']['end']} "
                f"USD {_money(sample['current']['value'])}；同比期间 "
                f"{sample['prior']['start']}..{sample['prior']['end']} "
                f"USD {_money(sample['prior']['value'])}。"
            ),
            (
                f"- 结果：同比 `{sample['growth_percent']}%`；concept "
                f"`{sample['taxonomy']}:{sample['concept']}`；source verifier / "
                "numeric verifier 均为 `pass`。"
            ),
            (
                f"- Formal Claim：`{sample['formal_claim_ref']}`；authority hash "
                f"`{sample['authority_hash']}`。"
            ),
            "- Thesis impact：`not yet run`；实际模型成本：`USD 0.00`。",
            "",
        ])
    control = batch["fail_closed_sample"]
    lines.extend([
        "## Fail-closed control",
        "",
        (
            f"Walmart 只允许 stale concept `{control['concept_candidates'][0]}`。"
            f"执行结果为 `{control['status']}` / `{control['failure_status']}`，"
            "正式 Evidence、Claim、Thesis 均为 0，未生成 candidate。"
        ),
        "",
        "## 重放",
        "",
        "每家公司可在 bundle 根目录外执行：",
        "",
        "```bash",
        (
            "python scripts/replay_sec_research_plan_canary.py "
            "--output-dir <BUNDLE_DIR>/samples/TICKER"
        ),
        "```",
        "",
        "重放只重读持久化 authority，不访问网络；成功时 closure 必须返回 `duplicate`。",
        "",
        "本简报只验证正式财务 Claim 的同口径复现，不构成投资建议。",
        "",
    ])
    return "\n".join(lines)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _git_state() -> tuple[str, bool]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return head, dirty


def _run_child(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=300,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--filed-from", required=True)
    parser.add_argument("--filed-to", required=True)
    parser.add_argument("--policy-owner", required=True)
    parser.add_argument(
        "--user-agent",
        default="Dalton Research Agent Gate 1 SEC revenue-growth batch",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    if not args.policy_owner.startswith("human:"):
        parser.error("--policy-owner must use the human: namespace")
    head, dirty = _git_state()
    if dirty and not args.allow_dirty:
        parser.error("repository must be clean; commit the exact batch code first")
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        parser.error("--output-dir must not already exist")
    output_dir.mkdir(parents=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    samples_dir = output_dir / "samples"
    samples_dir.mkdir(mode=0o700)

    samples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for issuer in ISSUERS:
        child = samples_dir / issuer["ticker"]
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_sec_research_plan_canary.py"),
            "--output-dir", str(child),
            "--issuer-cik", issuer["cik"],
            "--company-ref", f"company:sec-cik:{_padded_cik(issuer['cik'])}",
            "--operation", "get_company_facts",
            "--filed-from", args.filed_from,
            "--filed-to", args.filed_to,
            "--policy-owner", args.policy_owner,
            "--user-agent", args.user_agent,
        ]
        completed = _run_child(command)
        if completed.returncode != 0:
            failures.append({
                "ticker": issuer["ticker"],
                "stage": "run",
                "returncode": completed.returncode,
                "stderr_tail": completed.stderr[-2000:],
            })
            continue
        try:
            result_path = child / "result.json"
            result = _load_json(result_path)
            replay_command = [
                sys.executable,
                str(REPO_ROOT / "scripts" / "replay_sec_research_plan_canary.py"),
                "--output-dir", str(child),
            ]
            replayed = _run_child(replay_command)
            if replayed.returncode != 0:
                raise BatchVerificationError(
                    f"offline replay failed: {replayed.stderr[-2000:]}"
                )
            replay_payload = json.loads(replayed.stdout.strip().splitlines()[-1])
            _write_json(child / "replay.json", replay_payload)
            samples.append(
                summarize_success(result, issuer=issuer, result_path=result_path)
            )
        except (BatchVerificationError, json.JSONDecodeError, IndexError) as exc:
            failures.append({
                "ticker": issuer["ticker"],
                "stage": "verify",
                "error": str(exc),
            })
        time.sleep(0.25)

    control_dir = samples_dir / FAIL_CLOSED_SAMPLE["ticker"]
    control_command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "run_sec_research_plan_canary.py"),
        "--output-dir", str(control_dir),
        "--issuer-cik", FAIL_CLOSED_SAMPLE["cik"],
        "--company-ref",
        f"company:sec-cik:{_padded_cik(FAIL_CLOSED_SAMPLE['cik'])}",
        "--operation", "get_company_facts",
        "--concept-candidate", FAIL_CLOSED_SAMPLE["concept_candidates"][0],
        "--filed-from", args.filed_from,
        "--filed-to", args.filed_to,
        "--policy-owner", args.policy_owner,
        "--user-agent", args.user_agent,
        "--expect-blocked",
    ]
    control_run = _run_child(control_command)
    control_summary: dict[str, Any] | None = None
    if control_run.returncode != 0:
        failures.append({
            "ticker": FAIL_CLOSED_SAMPLE["ticker"],
            "stage": "run",
            "returncode": control_run.returncode,
            "stderr_tail": control_run.stderr[-2000:],
        })
    else:
        try:
            control_result = _load_json(control_dir / "result.json")
            control_summary = validate_fail_closed_result(
                control_result,
                expected_concepts=FAIL_CLOSED_SAMPLE["concept_candidates"],
            )
            control_summary.update({
                "ticker": FAIL_CLOSED_SAMPLE["ticker"],
                "name": FAIL_CLOSED_SAMPLE["name"],
                "result_path": (
                    f"samples/{FAIL_CLOSED_SAMPLE['ticker']}/result.json"
                ),
                "result_sha256": _sha256(control_dir / "result.json"),
            })
        except BatchVerificationError as exc:
            failures.append({
                "ticker": FAIL_CLOSED_SAMPLE["ticker"],
                "stage": "verify",
                "error": str(exc),
            })

    generated_at = _wire_time()
    batch: dict[str, Any] = {
        "schema_version": "0.1",
        "status": (
            "complete"
            if len(samples) == len(ISSUERS) and control_summary is not None and not failures
            else "failed"
        ),
        "generated_at": generated_at,
        "repo_commit": head,
        "repo_dirty": dirty,
        "filed_from": args.filed_from,
        "filed_to": args.filed_to,
        "source": "SEC data.sec.gov Company Facts",
        "samples": samples,
        "fail_closed_sample": control_summary,
        "failures": failures,
        "replay_command": [
            "python",
            "scripts/replay_sec_research_plan_canary.py",
            "--output-dir", "<BUNDLE_DIR>/samples/TICKER",
        ],
    }
    identity = {
        key: batch[key]
        for key in (
            "repo_commit", "filed_from", "filed_to", "source", "samples",
            "fail_closed_sample", "failures", "replay_command",
        )
    }
    batch["bundle_hash"] = content_hash(identity)
    _write_json(output_dir / "batch-result.json", batch)
    if batch["status"] == "complete":
        brief = render_brief(batch)
        brief_path = output_dir / "brief.md"
        brief_path.write_text(brief, encoding="utf-8")
        os.chmod(brief_path, 0o600)
    print(json.dumps(batch, ensure_ascii=False, sort_keys=True))
    return 0 if batch["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
