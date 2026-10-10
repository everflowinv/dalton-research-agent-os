#!/usr/bin/env python3
"""Read-only replay of the 2026-09-29 admission rules over what batch g admitted.

    PYTHONPATH=src .venv/bin/python scripts/replay_claim_scope_2026_09_29.py \\
        --state-dir <state-dir> \\
        --state-dir <state-dir> \\
        --since 2026-09-29T11:00 [--out /tmp/claim-scope-replay.json]

Opens every Core with ``mode=ro`` and reads only the small spool objects of
the Guidepoint excerpts it inspects; writes nothing but ``--out``.  Per
environment, over the live qualitative Claims admitted since ``--since``:

* ``same_span_rereads`` -- a Claim on the (document, subject, cited span) of
  a live Claim admitted by an earlier read (>= 5 minutes before): what the
  admission scope's same-span rule now makes a duplicate (DXC d50efc...);
* ``same_finding_in_read`` -- groups of Claims of one document and subject,
  admitted since ``--since``, that ``same_finding`` calls one finding; the
  earliest is kept, the rest listed (EPAM's 10-K);
* ``interviewer_turn`` -- Guidepoint Claims whose whole cited span is the
  interviewer's question (``claim_support_context.speaker_at``, contract v5);
* ``marketing_url`` -- Claims cited from the given page URLs (url-sha256);
* ``scope_replay`` -- per document with more than three Claims admitted
  since ``--since``: what ``plan_document_admissions`` would have admitted
  had those Claims been the candidates, against the live Claims the document
  had before (admit / duplicate / truncated), and how many period spellings
  they carry.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dalton_core.claim_support_context import speaker_at
from dalton_core.document_admission_scope import (
    ledger_bindings, plan_document_admissions, same_finding,
)

DEFAULT_MARKETING_URLS = ("https://agent.nexus/blog/cognizant-ai-alternatives",)
REREAD_GAP = timedelta(minutes=5)
_SPOOLS = ("connector-spool/connector-spool", "transcript-spool/connector-spool",
           "raw-spool/connector-spool")


def _connect(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _spool_text(state: Path, digest: str) -> str | None:
    for spool in _SPOOLS:
        path = state / spool / "objects" / digest[:2] / digest
        if path.is_file() and path.stat().st_size < 2_000_000:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                return None
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def replay(state: Path, since: str, marketing_urls: tuple[str, ...]) -> dict:
    connection = _connect(state)
    try:
        ledger = ledger_bindings(connection)
        claims = {row["claim_version_id"]: row for row in connection.execute(
            "SELECT claim_version_id, created_at, claim_json FROM claim_versions "
            "WHERE json_extract(claim_json,'$.claim_kind')='qualitative'")}
        document_refs = {}
        for row in connection.execute(
                "SELECT b.source_content_hash AS digest, c.record_json AS record "
                "FROM transcript_claim_citation_bindings b JOIN transcript_correction_set_versions c "
                "ON c.version_id=b.correction_set_version_ref"):
            document_refs.setdefault(row["digest"], json.loads(row["record"]).get("document_ref"))
    finally:
        connection.close()
    since_time = _time(since if "+" in since or since.endswith("Z") else since + "+00:00")
    out = {"state_dir": str(state), "since": since, "same_span_rereads": [],
           "same_finding_in_read": [], "interviewer_turn": [], "marketing_url": [],
           "scope_replay": []}
    marketing = {hashlib.sha256(url.encode()).hexdigest(): url for url in marketing_urls}

    def created(ref: str) -> datetime:
        return _time(claims[ref]["created_at"])

    def body(ref: str) -> dict:
        return json.loads(claims[ref]["claim_json"])

    for digest, rows in ledger.items():
        rows = [row for row in rows if row["ref"] in claims]
        recent = [row for row in rows if created(row["ref"]) >= since_time]
        if not recent:
            continue
        document_ref = document_refs.get(digest)
        # -- the same span, read again --------------------------------------------
        for row in recent:
            earlier = [other for other in rows if other["ref"] != row["ref"]
                       and other["subject_ref"] == row["subject_ref"]
                       and (other["start"], other["end"]) == (row["start"], row["end"])
                       and created(other["ref"]) <= created(row["ref"]) - REREAD_GAP]
            if earlier:
                first = min(earlier, key=lambda other: created(other["ref"]))
                out["same_span_rereads"].append({
                    "claim_version_ref": row["ref"], "duplicate_of": first["ref"],
                    "document_ref": document_ref, "span": [row["start"], row["end"]],
                    "created_at": claims[row["ref"]]["created_at"],
                    "duplicate_created_at": claims[first["ref"]]["created_at"],
                    "statement": row["statement"][:300]})
        # -- one finding, several Claims, in the recent reads -----------------------
        by_subject = collections.defaultdict(list)
        for row in recent:
            by_subject[row["subject_ref"]].append(row)
        for subject, group in by_subject.items():
            group.sort(key=lambda row: (created(row["ref"]), row["ref"]))
            parent = {row["ref"]: row["ref"] for row in group}

            def root(ref: str) -> str:
                while parent[ref] != ref:
                    ref = parent[ref]
                return ref

            pairs = []
            for index, first in enumerate(group):
                for second in group[index + 1:]:
                    similarity = same_finding(first["statement"], second["statement"])
                    if similarity is not None:
                        pairs.append((first["ref"], second["ref"], similarity))
                        a, b = root(first["ref"]), root(second["ref"])
                        if a != b:
                            keep, drop = sorted((a, b), key=lambda ref: (created(ref), ref))
                            parent[drop] = keep
            groups = collections.defaultdict(list)
            for row in group:
                groups[root(row["ref"])].append(row)
            for keep, members in groups.items():
                if len(members) < 2:
                    continue
                out["same_finding_in_read"].append({
                    "document_ref": document_ref, "subject_ref": subject, "keep": keep,
                    "keep_statement": next(m["statement"] for m in members if m["ref"] == keep)[:300],
                    "withdraw": [{"claim_version_ref": m["ref"], "statement": m["statement"][:300],
                                  "similarity": max(p[2] for p in pairs if m["ref"] in p[:2])}
                                 for m in members if m["ref"] != keep],
                    "pairs": [list(p) for p in pairs
                              if p[0] in {m["ref"] for m in members}]})
        # -- an interviewer's question filed as an answer ---------------------------
        if str(document_ref or "").startswith("guidepoint-excerpt:"):
            text = _spool_text(state, digest)
            for row in recent if text else ():
                start = speaker_at(text, row["start"])
                end = speaker_at(text, max(row["start"], row["end"] - 1))
                if start and start.startswith("interviewer") and end and end.startswith("interviewer"):
                    out["interviewer_turn"].append({
                        "claim_version_ref": row["ref"], "document_ref": document_ref,
                        "subject_ref": row["subject_ref"], "span": [row["start"], row["end"]],
                        "cited_text": text[row["start"]:row["end"]][:400],
                        "statement": row["statement"][:300]})
        # -- a vendor's marketing page ---------------------------------------------
        url_hash = str(document_ref or "").split("url-sha256:", 1)[-1][:64]
        if url_hash in marketing:
            for row in recent:
                claim = body(row["ref"])
                out["marketing_url"].append({
                    "claim_version_ref": row["ref"], "url": marketing[url_hash],
                    "subject_ref": row["subject_ref"], "period": claim.get("period"),
                    "statement": row["statement"][:300]})
        # -- what the admission scope would have done -------------------------------
        if len(recent) > 3:
            before = [row for row in rows if created(row["ref"]) < since_time]
            candidates = [{"key": row["ref"], "subject_ref": row["subject_ref"],
                           "statement": row["statement"], "start": row["start"], "end": row["end"]}
                          for row in sorted(recent, key=lambda row: (row["start"], row["ref"]))]
            plan = plan_document_admissions(candidates, before)
            decisions = collections.Counter(
                value["decision"] + (":" + value["basis"] if value["decision"] == "duplicate" else "")
                for value in plan.values())
            out["scope_replay"].append({
                "document_ref": document_ref, "admitted_since": len(recent),
                "live_before": len(before), "would_admit": decisions.get("admit", 0),
                "decisions": dict(decisions),
                "period_spellings": len({body(row["ref"]).get("period") for row in recent})})
    out["scope_replay"].sort(key=lambda item: -item["admitted_since"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", action="append", required=True, type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--marketing-url", action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    urls = tuple(args.marketing_url) or DEFAULT_MARKETING_URLS
    results = [replay(state.expanduser(), args.since, urls) for state in args.state_dir]
    for result in results:
        print(f"== {result['state_dir']} (since {result['since']})")
        for key in ("same_span_rereads", "same_finding_in_read", "interviewer_turn", "marketing_url"):
            print(f"  {key}: {len(result[key])}")
        for item in result["scope_replay"][:8]:
            print(f"  scope_replay {str(item['document_ref'])[:72]}: admitted {item['admitted_since']} "
                  f"(live before {item['live_before']}) -> would admit {item['would_admit']} "
                  f"{item['decisions']}; {item['period_spellings']} period spellings")
    if args.out is not None:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str),
                            encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
