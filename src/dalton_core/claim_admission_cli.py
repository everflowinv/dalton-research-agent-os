"""Replay today's admission-quality checks over admitted Claims; retire by hand.

    # read-only: which admitted Claims the 2026-09-25b admission checks would
    # have held, per check, with samples and a selection hash
    .venv/bin/python -m dalton_core.claim_admission_cli replay \\
        --state-dir "~/Library/Application Support/Dalton/state/dalton-core" \\
        [--since 2026-09-25T14:36] [--source-type public_web] [--show 10]

    # retire what one or more checks select: dry run first (the default) ...
    .venv/bin/python -m dalton_core.claim_admission_cli retire --state-dir "..." \\
        --from-replay statistics_compilation --expect-selection <sha256> \\
        --reason "SEO 统计汇编页，数字无原始出处"
    # ... then the same command with --apply --actor human:<name>

    # or name the Claims one by one
    .venv/bin/python -m dalton_core.claim_admission_cli retire --state-dir "..." \\
        --claim-version-ref claim-version:… --claim-version-ref claim-version:… \\
        --reason "…" [--apply --actor human:<name>]

``replay`` opens the Core read-only (``mode=ro``) and writes nothing.  The
checks it runs are the admission path's own (``claim_admission_quality`` and
``claim_subject``), on the exact cited span and the exact original re-read
from the spool, so its answer is what admission would do today:

* ``statistics_compilation`` -- a public-web page that is an SEO statistics
  compilation;
* ``temporal_impossibility`` -- a report cited for a period not over at the
  document's date, or an unfinished/future period stated as fact;
* ``relative_year`` -- a relative year that cannot be anchored, or that
  anchors to a year the Claim's period does not name;
* ``web_subject_strict`` -- a public-web span that never names the subject,
  on a page that is the subject's own only by its title or head (what the
  admission side used to accept);
* ``system_meta`` -- a statement about the research system's own process.

``retire --apply`` goes through the live writer's ephemeral human principal
(operation ``retire_claim_by_hand``): one ``human_judgment`` challenge and one
``retired`` decision per Claim, append-only, so every read path skips it and
a reinstatement can still undo it.  ``--from-replay`` recomputes the
selection and refuses to act unless it hashes to ``--expect-selection``, the
hash the dry run printed: what is retired is exactly what was reviewed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

from .claim_reinstatement_cli import _connect, _driver, _print

OPERATION = "retire_claim_by_hand"
CHECKS: tuple[str, ...] = (
    "statistics_compilation", "temporal_impossibility", "relative_year",
    "web_subject_strict", "system_meta",
)


def selection_hash(refs: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(refs))).encode("utf-8")).hexdigest()


def _evidence_by_claim(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT r.claim_version_id AS claim, e.evidence_json AS evidence "
        "FROM evidence_relations r JOIN evidence_versions e "
        "ON e.evidence_version_id=r.evidence_version_id WHERE r.relation='supports'"
    ).fetchall():
        try:
            found[row["claim"]] = json.loads(row["evidence"])
        except (TypeError, ValueError):
            continue
    return found


def _published(connection: sqlite3.Connection, document_ref: Any) -> tuple[Any, Any]:
    if not isinstance(document_ref, str):
        return None, None
    try:
        row = connection.execute(
            "SELECT title, published_at FROM document_provenance_records WHERE document_ref=?",
            (document_ref,),
        ).fetchone()
    except sqlite3.Error:
        return None, None
    return (None, None) if row is None else (row["title"], row["published_at"])


def judge_claim(
    *, claim: dict[str, Any], evidence: dict[str, Any], span: Any, text: Any,
    title: Any, published_at: Any, needles: list[str], peers: list[str],
) -> dict[str, str]:
    """check name -> reason, for every check that would hold this Claim today."""

    from .claim_admission_quality import (
        anchor_relative_years, statement_is_system_meta, statistics_compilation_evidence,
        statistics_compilation_hold, temporal_impossibility,
    )
    from .claim_subject import (
        document_is_subjects, span_names_subject_for_admission, web_span_names_subject_strictly,
    )

    statement = claim.get("normalized_statement")
    period = claim.get("period")
    web = evidence.get("source_type") == "public_web"
    if published_at:
        document_date, basis = str(published_at)[:10], "published"
    else:
        document_date = str(evidence.get("retrieved_at") or "")[:10] or None
        basis = "retrieved" if document_date else None
    found: dict[str, str] = {}
    if web and isinstance(text, str):
        page = statistics_compilation_evidence(text)
        if page is not None:
            found["statistics_compilation"] = statistics_compilation_hold(page)
    temporal = temporal_impossibility(statement=statement, period=period,
                                      document_date=document_date, cited_span=span)
    if temporal is not None:
        found["temporal_impossibility"] = temporal
    anchored = anchor_relative_years(statement=statement, period=period,
                                     document_date=document_date, date_basis=basis)
    if anchored["hold"] is not None:
        found["relative_year"] = anchored["hold"]
    elif anchored["anchored"] is not None and anchored["period"] != period:
        found["relative_year"] = (
            f"the statement's {anchored['anchored']['phrases']} is "
            f"{anchored['anchored']['year']} but the period says {period!r}")
    if web and needles and isinstance(span, str) and not str(
            claim.get("subject_ref", "")).startswith("industry:"):
        before = document_is_subjects(title=title, text=text, needles=needles)
        if (span_names_subject_for_admission(span=span, needles=needles, document_is_own=before)
                and not web_span_names_subject_strictly(
                    span=span, text=text, needles=needles,
                    subject_ref=claim.get("subject_ref"), peer_needles=peers)):
            found["web_subject_strict"] = (
                "the cited span never names the subject, and the page is its own only by "
                "its title or head")
    if statement_is_system_meta(statement):
        found["system_meta"] = "a statement about the research system's own process"
    return found


def replay(state: Path, *, since: str | None = None, source_type: str | None = None,
           show: int = 10, checks: Iterable[str] = CHECKS) -> dict[str, Any]:
    from .claim_retirement import retired_claim_version_refs

    wanted = tuple(checks)
    connection = _connect(state)
    try:
        driver = _driver(state, connection)
        roster = {ref: set(values) for ref, values in driver._roster_needles().items()}
        for ref, values in driver.needles.items():
            roster.setdefault(ref, set()).update(values)
        citations = driver._citations()
        evidence = _evidence_by_claim(connection)
        retired = retired_claim_version_refs(connection)
        query = "SELECT claim_version_id, claim_json, content_hash, created_at FROM claim_versions"
        params: tuple[Any, ...] = ()
        if since:
            query += " WHERE created_at>=?"
            params = (since,)
        rows = connection.execute(query + " ORDER BY created_at, claim_version_id",
                                  params).fetchall()
        texts: dict[str, Any] = {}
        summary: dict[str, Any] = {
            "state_dir": str(state), "since": since, "source_type": source_type,
            "checks": list(wanted), "examined": 0, "already_retired": 0, "unreadable": 0,
            "by_check": {name: 0 for name in wanted}, "selected": [],
        }
        samples: dict[str, list[dict[str, Any]]] = {name: [] for name in wanted}
        for row in rows:
            ref = row["claim_version_id"]
            ev = evidence.get(ref)
            if ev is None or (source_type and ev.get("source_type") != source_type):
                continue
            claim = json.loads(row["claim_json"])
            if claim.get("claim_kind") != "qualitative":
                continue
            if ref in retired:
                summary["already_retired"] += 1
                continue
            summary["examined"] += 1
            citation = citations.get(ref)
            text = span = None
            if citation is not None:
                digest = citation["digest"]
                if digest not in texts:
                    texts[digest] = driver.source_text(digest)
                text = texts[digest]
                start, end = citation.get("start"), citation.get("end")
                if (isinstance(text, str) and isinstance(start, int) and isinstance(end, int)
                        and 0 <= start < end <= len(text)):
                    span = text[start:end]
            if text is None:
                summary["unreadable"] += 1
            title, published_at = _published(
                connection, None if citation is None else citation.get("document_ref"))
            subject = str(claim.get("subject_ref") or "")
            needles = sorted(roster.get(subject, ()))
            peers = sorted({n for other, values in roster.items() if other != subject
                            for n in values})
            reasons = {name: reason for name, reason in judge_claim(
                claim=claim, evidence=ev, span=span, text=text, title=title,
                published_at=published_at, needles=needles, peers=peers,
            ).items() if name in wanted}
            if not reasons:
                continue
            item = {"claim_version_ref": ref, "claim_version_hash": row["content_hash"],
                    "created_at": row["created_at"], "subject_ref": subject,
                    "source_type": ev.get("source_type"), "period": claim.get("period"),
                    "statement": str(claim.get("normalized_statement") or "")[:240],
                    "document_ref": None if citation is None else citation.get("document_ref"),
                    "title": (title or (text.splitlines()[0] if isinstance(text, str) and text
                                        else None) or "")[:120],
                    "checks": sorted(reasons), "reasons": reasons}
            summary["selected"].append(item)
            for name in reasons:
                summary["by_check"][name] += 1
                if len(samples[name]) < max(0, int(show)):
                    samples[name].append({key: item[key] for key in (
                        "claim_version_ref", "subject_ref", "period", "statement", "title")}
                        | {"reason": reasons[name]})
    finally:
        connection.close()
    summary["selected_count"] = len(summary["selected"])
    summary["selection_sha256"] = selection_hash(
        item["claim_version_ref"] for item in summary["selected"])
    summary["samples"] = samples
    return summary


def retire(state: Path, *, claim_version_refs: Iterable[str] = (),
           from_replay: Iterable[str] = (), since: str | None = None,
           source_type: str | None = None, expect_selection: str | None = None,
           reason: str, apply: bool, actor: str | None) -> dict[str, Any]:
    from .claim_retirement import retired_claim_version_refs

    explicit = sorted(set(claim_version_refs))
    checks = [name for name in from_replay if name]
    if bool(explicit) == bool(checks):
        raise SystemExit("name Claims with --claim-version-ref or select them with --from-replay")
    unknown = sorted(set(checks) - set(CHECKS))
    if unknown:
        raise SystemExit(f"--from-replay: unknown check(s) {unknown}; one of {list(CHECKS)}")
    if checks:
        summary = replay(state, since=since, source_type=source_type, show=0, checks=checks)
        targets = [(item["claim_version_ref"], item["claim_version_hash"], item["statement"],
                    item["checks"]) for item in summary["selected"]]
        digest = summary["selection_sha256"]
        if expect_selection is not None and expect_selection != digest:
            raise SystemExit(
                f"the selection changed since it was reviewed: {digest} != {expect_selection}")
        if apply and expect_selection is None:
            raise SystemExit("--apply with --from-replay needs --expect-selection from the dry run")
    else:
        connection = _connect(state)
        try:
            targets = []
            for ref in explicit:
                row = connection.execute(
                    "SELECT claim_json, content_hash FROM claim_versions WHERE claim_version_id=?",
                    (ref,),
                ).fetchone()
                if row is None:
                    raise SystemExit(f"no claim version {ref}")
                claim = json.loads(row["claim_json"])
                targets.append((ref, row["content_hash"],
                                str(claim.get("normalized_statement") or "")[:240], []))
            retired = retired_claim_version_refs(connection)
        finally:
            connection.close()
        targets = [item for item in targets if item[0] not in retired]
        digest = selection_hash(item[0] for item in targets)
    preview = [{"claim_version_ref": ref, "statement": statement, "checks": names}
               for ref, _hash, statement, names in targets]
    if not apply:
        return {"status": "dry_run", "count": len(targets), "selection_sha256": digest,
                "claims": preview}
    if not actor or not actor.startswith("human:"):
        raise SystemExit("--apply needs --actor human:<name>")
    from .governance_cli import ephemeral_call

    results = []
    for ref, claim_hash, _statement, _names in targets:
        try:
            result = ephemeral_call(
                state / "writer-tokens.json", state / "run" / "writer.sock",
                actor_ref=actor, operation=OPERATION,
                params={"claim_version_ref": ref, "claim_version_hash": claim_hash,
                        "rationale": reason, "actor_ref": actor},
            )
        except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest
            result = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        results.append({"claim_version_ref": ref, "result": result})
    return {"status": "applied", "count": len(targets), "selection_sha256": digest,
            "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claim_admission_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    rp = sub.add_parser("replay", help="read-only: what today's admission checks would hold")
    rp.add_argument("--state-dir", required=True)
    rp.add_argument("--since")
    rp.add_argument("--source-type")
    rp.add_argument("--show", type=int, default=10)
    rp.add_argument("--check", action="append", choices=CHECKS)
    rp.add_argument("--list", action="store_true", help="print every selected Claim")
    rt = sub.add_parser("retire", help="retire admitted Claims by hand (dry run by default)")
    rt.add_argument("--state-dir", required=True)
    rt.add_argument("--claim-version-ref", action="append", default=[])
    rt.add_argument("--from-replay", action="append", default=[], choices=CHECKS)
    rt.add_argument("--since")
    rt.add_argument("--source-type")
    rt.add_argument("--expect-selection")
    rt.add_argument("--reason", required=True)
    rt.add_argument("--apply", action="store_true")
    rt.add_argument("--actor")
    args = parser.parse_args(argv)
    state = Path(args.state_dir).expanduser()
    if args.command == "replay":
        summary = replay(state, since=args.since, source_type=args.source_type,
                         show=args.show, checks=args.check or CHECKS)
        if not args.list:
            summary["selected"] = [
                {key: item[key] for key in ("claim_version_ref", "subject_ref", "checks")}
                for item in summary["selected"]]
        _print(summary)
        return 0
    _print(retire(state, claim_version_refs=args.claim_version_ref,
                  from_replay=args.from_replay, since=args.since,
                  source_type=args.source_type, expect_selection=args.expect_selection,
                  reason=args.reason, apply=args.apply, actor=args.actor))
    return 0


if __name__ == "__main__":
    sys.exit(main())
