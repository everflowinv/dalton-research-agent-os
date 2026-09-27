"""Replay a day's ``not_supported`` support verdicts under the v2 question -- read-only, no model.

    PYTHONPATH=src .venv/bin/python scripts/replay_claim_support_v2.py \\
        --env legacy=/Volumes/EveSSD/Dalton/legacy-state/dalton-core \\
        --env ws7d=/Volumes/EveSSD/Dalton/workspaces/ws-7d.../state/dalton-core \\
        --since 2026-09-26 --labels scripts/replay_claim_support_v2_labels.json --out /tmp/replay.json

For every verdict of either support purpose since ``--since`` that said
``not_supported``, it rebuilds the question the current contract asks
(``claim_support_context``: whole sentences around the citation, the document's
title/date/house, the statement's period, the transcript speaker) exactly as
the backfill and the recheck build it, and runs the batch through a
``ClaimSupportVerifier`` on an in-memory store whose model is a local mock:
the mock answers each item with the verdict a person gave it in ``--labels``
(keyed ``<env>:<first 12 hex of the v1 item key>``), and ``not_supported`` for
an item nobody labelled.  So the replay shows what the new question contains,
whether the prompt stays in bounds, and what the pipeline does with the expected
answers (which retired Claims the re-review would reinstate) -- it does not,
and cannot, stand in for the model.

Every live database is opened ``mode=ro``; nothing is written anywhere but the
in-memory store and ``--out``.  No network, no model call.

2026-09-27 (contract v3).  ``--second-opinion`` replays the two-family rule:
the mock's *first* answer for every item is the live verdict it replays (the
rejection the first family actually gave, so the replay assumes the new
wording changes nothing about it), and its *second* answer -- the call the
verifier makes only for a first answer that does not admit -- is the person's
label.  So it shows which rejections the second opinion would overturn if the
other family judged as the person did, how many extra calls and bytes that
costs, and that nothing labelled a true rejection is admitted.  It cannot say
whether a real second model would judge that way.  ``--extra-labels`` adds
label files (the day's new items); a label is also found by its statement, so
a statement labelled under one contract labels its re-ask under the next.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _prompt_items(scheduler: sqlite3.Connection, work_order_ref: str) -> list[dict[str, Any]]:
    row = scheduler.execute(
        "SELECT json_extract(work_order_json,'$.question') FROM scheduler_work_orders WHERE work_order_id=?",
        (work_order_ref,)).fetchone()
    question = row[0] if row else None
    if not isinstance(question, str) or "UNTRUSTED_ITEMS=" not in question:
        return []
    return json.loads(question[question.rindex("UNTRUSTED_ITEMS=") + len("UNTRUSTED_ITEMS="):])


def replay_env(name: str, state: Path, since: str) -> list[dict[str, Any]]:
    from dalton_core import claim_reinstatement_cli as cli
    from dalton_core.claim_support_backfill import ClaimSupportBackfill
    from dalton_core.claim_support_recheck import ClaimSupportRecheck

    core = cli._connect(state)
    scheduler = _ro(state / "scheduler.sqlite")
    staging = _ro(state / "research-review" / "candidate-staging.sqlite")
    driver = cli._driver(state, core)
    citations = driver._citations()
    missions = cli._MissionReader(core)
    pointer = core.execute("SELECT mission_version_id FROM coverage_mission_pointer "
                           "ORDER BY mission_ref LIMIT 1").fetchone()
    mission = missions.mission(pointer[0])
    backfill = ClaimSupportBackfill(
        store=SimpleNamespace(connection=core), missions=missions, verifier=None, reader=driver,
        challenges=None, claim_sources=())
    names = backfill._subject_names(mission)
    recheck = ClaimSupportRecheck.__new__(ClaimSupportRecheck)
    recheck.connection, recheck.spool, recheck._texts = core, driver.spool, {}
    retired = {row[0] for row in core.execute(
        "SELECT h.claim_version_ref FROM claim_retirement_challenges h JOIN claim_retirement_decisions d "
        "ON d.challenge_ref=h.challenge_id WHERE h.reason_code='citation_support_rejected' "
        "AND d.decision='retired'")}
    out = []
    for verdict in core.execute(
            "SELECT * FROM claim_support_verdicts WHERE created_at>=? AND support='not_supported' "
            "ORDER BY created_at, item_key", (since,)).fetchall():
        asked = next((item for item in _prompt_items(scheduler, verdict["work_order_ref"])
                      if _sha(item["statement"]) == verdict["statement_sha256"]), None)
        entry: dict[str, Any] = {
            "env": name, "label_key": f"{name}:{verdict['item_key'][:12]}",
            "purpose": verdict["purpose"], "v1_item_key": verdict["item_key"],
            "subject_ref": verdict["subject_ref"], "v1_subject_relation": verdict["subject_relation"],
            "v1_other_subject": verdict["other_subject"],
            "contract_ref": verdict["contract_ref"],
            "statement": asked and asked["statement"], "v1_cited_chars": asked and len(asked["cited_text"]),
            "v1_cited_complete": bool(asked and _sha(asked["cited_text"]) == verdict["cited_sha256"]),
        }
        item = None
        if asked is not None and verdict["purpose"] == "claim_support_backfill":
            mark = core.execute("SELECT claim_version_ref FROM claim_support_backfill_marks "
                                "WHERE item_key=? LIMIT 1", (verdict["item_key"],)).fetchone()
            ref = mark and mark[0]
            entry["claim_version_ref"] = ref
            entry["retired"] = ref in retired
            citation = citations.get(ref)
            claim_row = core.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?",
                                     (ref,)).fetchone()
            text = citation and driver.source_text(citation["digest"])
            if claim_row is not None and text:
                item = backfill._item(json.loads(claim_row[0]), citation, text, names)
        elif asked is not None:
            row = staging.execute(
                "SELECT c.version_id, c.record_json, e.record_json AS evidence "
                "FROM candidate_claim_versions c JOIN candidate_evidence_versions e "
                "ON e.version_id=c.evidence_version_id "
                "WHERE json_extract(c.record_json,'$.normalized_statement')=? "
                "AND json_extract(c.record_json,'$.subject_ref')=? ORDER BY c.created_at DESC LIMIT 1",
                (asked["statement"], verdict["subject_ref"])).fetchone()
            if row is not None:
                entry["candidate_claim_ref"] = row["version_id"]
                binding = recheck._binding(json.loads(row["evidence"]))
                if binding is not None:
                    item = recheck._item(json.loads(row["record_json"]), json.loads(row["evidence"]),
                                         binding, asked)
        if item is None and asked is not None:
            from dalton_core.claim_support_verification import support_item

            item = support_item(subject_ref=verdict["subject_ref"], subject_name=asked.get("subject"),
                                statement=asked["statement"], cited_text=asked["cited_text"],
                                producer_route_ref="route-decision:replay")
            entry["rebuilt_from"] = "v1 prompt (original not readable)"
        entry["v2_item"] = item
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", action="append", required=True, help="name=/path/to/state/dalton-core")
    parser.add_argument("--since", required=True)
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--extra-labels", type=Path, action="append", default=[])
    parser.add_argument("--second-opinion", action="store_true",
                        help="replay the v3 two-family rule: first answer = the live verdict, "
                             "second answer = the label")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    from dalton_core.claim_support_verification import (
        BACKFILL_PURPOSE, PURPOSE, ClaimSupportVerifier, admissible, build_prompt)
    from dalton_core.store import DaltonStore

    labels = json.loads(args.labels.read_text(encoding="utf-8")) if args.labels else {}
    for extra in args.extra_labels:
        labels.update(json.loads(extra.read_text(encoding="utf-8")))
    by_label_statement = {str(value.get("statement") or "")[:200]: value for value in labels.values()
                          if value.get("statement")}
    entries: list[dict[str, Any]] = []
    for spec in args.env:
        name, _, path = spec.partition("=")
        entries += replay_env(name, Path(path).expanduser(), args.since)

    by_statement = {}
    for entry in entries:
        label = labels.get(entry["label_key"])
        if label is None and entry.get("statement"):
            label = by_label_statement.get(entry["statement"][:200])
            if label is not None:
                entry["label_by_statement"] = True
        entry["labelled"] = label is not None
        label = label or {}
        entry["expected"] = label.get("v2", "not_supported")
        entry["subject_expected"] = label.get("subject", entry["v1_subject_relation"])
        entry["reason"] = label.get("reason")
        entry["category"] = label.get("category")
        if entry["v2_item"] is not None:
            by_statement[entry["v2_item"]["statement"]] = entry

    second_calls: list[int] = []

    def mock_model(**kwargs):  # the local mock: the labelled answer, never a model
        prompt = kwargs["prompt"]
        items = json.loads(prompt[prompt.rindex("UNTRUSTED_ITEMS=") + len("UNTRUSTED_ITEMS="):])
        second = ":second:" in str(kwargs.get("request_id"))
        if second:
            second_calls.append(len(prompt.encode("utf-8")))
        verdicts = []
        for index, item in enumerate(items):
            entry = by_statement[item["statement"]]
            if args.second_opinion and not second:
                # The first family: what it actually answered, live.
                support, subject = "not_supported", entry["v1_subject_relation"]
            else:
                support, subject = entry["expected"], entry["subject_expected"]
            verdicts.append({"item_id": f"i{index + 1}", "support": support,
                             "subject": subject, "other_subject": None})
        return {"text": json.dumps({"schema_version": "0.1", "verdicts": verdicts}), "cost_micros": 0,
                "work_order_ref": "work:replay-mock",
                "route_decision_ref": "route-decision:replay-second" if second else "route-decision:replay-first",
                "invocation_ref": None}

    store = DaltonStore(":memory:")
    summary: dict[str, Any] = {"items": len(entries), "prompts": []}
    for purpose, per_call in ((PURPOSE, 12), (BACKFILL_PURPOSE, 20)):
        mine = [e for e in entries if e["purpose"] == purpose and e["v2_item"] is not None]
        if not mine:
            continue
        verifier = ClaimSupportVerifier(
            store=store, model_call=mock_model, purpose=purpose, daily_cap_micros=10 ** 9,
            producer_family=lambda ref: "replay", max_items_per_call=per_call,
            second_opinion=args.second_opinion, second_opinion_route=lambda families: None)
        items = [e["v2_item"] for e in mine]
        for chunk in verifier._chunks(items):
            summary["prompts"].append({"purpose": purpose, "items": len(chunk),
                                       "bytes": len(build_prompt(chunk).encode("utf-8"))})
        outcome = verifier.verify(mission={"id": "replay"}, items=items)
        for entry in mine:
            verdict = outcome["verdicts"].get(entry["v2_item"]["item_key"])
            entry["v2_admissible"] = bool(verdict and admissible(verdict))
            if verdict and "second_opinion" in verdict:
                entry["second_opinion"] = verdict["second_opinion"].get("support")
    for entry in entries:
        item = entry.pop("v2_item")
        if item is not None:
            entry["v2_cited_chars"] = len(item["cited_text"])
            entry["v2_document"] = item["document"]
            entry["v2_cited_text"] = item["cited_text"]
        entry["would_reinstate"] = bool(entry.get("retired") and entry.get("v2_admissible"))
    summary["v1_not_supported"] = len(entries)
    summary["by_contract"] = {}
    for entry in entries:
        bucket = summary["by_contract"].setdefault(entry["contract_ref"], {
            "not_supported": 0, "labelled": 0, "labelled_supported": 0, "admissible_now": 0,
            "true_rejection_admitted": 0})
        bucket["not_supported"] += 1
        bucket["labelled"] += int(entry["labelled"])
        bucket["labelled_supported"] += int(entry["labelled"] and entry["expected"] == "supported")
        bucket["admissible_now"] += int(bool(entry.get("v2_admissible")))
        bucket["true_rejection_admitted"] += int(bool(
            entry.get("v2_admissible") and entry["labelled"] and entry["expected"] != "supported"))
    summary["second_opinion_calls"] = len(second_calls)
    summary["second_opinion_prompt_bytes"] = sum(second_calls)
    summary["first_prompt_bytes"] = sum(p["bytes"] for p in summary["prompts"])
    summary["labelled"] = sum(1 for e in entries if e["label_key"] in labels)
    summary["v2_supported"] = sum(1 for e in entries if e["expected"] == "supported")
    summary["v2_admissible"] = sum(1 for e in entries if e.get("v2_admissible"))
    summary["retired"] = sum(1 for e in entries if e.get("retired"))
    summary["would_reinstate"] = [e["claim_version_ref"] for e in entries if e["would_reinstate"]]
    summary["widened"] = sum(1 for e in entries
                             if (e.get("v2_cited_chars") or 0) > (e.get("v1_cited_chars") or 0))
    args.out.write_text(json.dumps({"summary": summary, "entries": entries}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
