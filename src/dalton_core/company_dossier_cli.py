"""P12a child: draft the stalest parts of one company's dossier and publish.

Out of process, like every lane that calls a model, because the writer abandons
a request after 30 seconds and a drafting call is allowed 180.

One run does six things and stops:

1. check the grant before spending anything -- drafting and then being refused
   costs the mission real model calls;
2. choose one company that has passed its Initial Screen and has new canonical
   Claims for some part of its file since the last version;
3. decide, per unit, whether it can be drafted at all: an unmapped causal
   chain, an absent calendar authority, an absent price series and an empty
   aspect are four different reasons a section is *unavailable*, and none of
   them is a reason to write something;
4. draft the stalest few units, one bounded call each, refusing any reply that
   leaves its contract;
5. verify the whole draft once, with a model of a different family, and run
   Q1's ``company-dossier`` rubric and the Constitution's ``output_rubric``
   over it;
6. publish -- where ADR-0008 decides whether this is a version at all.

Exit 0 when the run completed, including when it decided nothing needed doing.

``formal_authority_writes`` is always 0.  A dossier is assembled *from* Claims
and never writes one; the Ledger is not opened for writing here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .cockpit_model import CockpitModel
from .call_budget import resolve_call_budget, resolve_run_budget
from .company_dossier import (
    CLASSIFICATION_UNIT,
    SECTIONS,
    UNITS,
    VARIANT_UNIT,
    CompanyDossierAuthority,
    CompanyDossierError,
    DossierStructureUnmapped,
    WRITE_SCOPE,
    dossier_artefact,
    evidence_scope,
    load_policy,
    new_refs,
    output_rubric_findings,
    policy_hash,
    section_body,
    unit_slots,
    _unit_was_drafted,
)
from .company_dossier_draft import (
    MAX_CLAIM_ROWS,
    independence_precheck,
    MAX_COST_USD,
    MAX_INPUT_TOKENS,
    MAX_NUMBER_ROWS,
    MAX_OUTPUT_TOKENS,
    MAX_RUN_COST_USD,
    MAX_UNITS_PER_RUN,
    TIMEOUT_SECONDS,
    build_unit_prompt,
    draft_unit,
    independence,
    material_rows,
    router_family_resolver,
    summarise_blocks,
    verify,
)
from .coverage_mission import CoverageMissionAuthority, fold_stage_status
from .guidance_profile import build_profile, render_profile_table
from .store import DaltonStore, canonical_json

SUMMARY_SCHEMA_VERSION = "0.1"
# The two deterministic checks a draft may not fail. Everything else the rubric
# reports is recorded and read; these two are the blueprint's stop-loss for
# this layer, so they are a gate rather than a score.
HARD_CHECKS: tuple[str, ...] = ("numbers_without_refs", "new_version_cites_new_refs")
MAX_STATEMENT_PERIODS = 8
# The sections a filed figure belongs beside. The rest of the file is about
# judgement, and a number offered to a section that cannot use it is prompt
# budget spent on distraction.
FIGURE_SECTIONS: tuple[str, ...] = (
    "business_model", "segments_and_mix", "demand_drivers", "supply_and_cost",
)
# The share of the figure bound reserved for the driver model's cells.
MAX_FORECAST_ROWS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_owner_only(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def granted_scope(mission: Mapping[str, Any]) -> str | None:
    """The word that lets this run write, or None.

    No fallback. ``dossier`` is already in the frozen vocabulary (Wave 0 added
    it), so a mission that has not granted it has not granted this -- and a run
    that borrowed ``deliverable`` would be writing an authority the owner never
    authorised under a word that means something else.
    """

    may_write = set((mission.get("autonomy") or {}).get("may_write") or [])
    return WRITE_SCOPE if WRITE_SCOPE in may_write else None


def table_exists(connection: Any, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def screened_companies(missions: Any, mission: Mapping[str, Any]) -> list[str]:
    """Companies whose Initial Screen has passed, in mission priority order.

    The dossier is the layer above the screen: a company nobody has screened
    has no file to deepen, and drafting one would spend model calls on the
    material the screen exists to assemble.
    """

    # P14-S: folded across every version of the mission_ref. A gate that
    # passed under v13 is still passed under v14; ``stage_records(mission_id)``
    # said otherwise and would have stopped every dossier on the next publish.
    state = missions.stage_state_by_company(mission["mission_ref"])
    passed = {
        company_ref
        for company_ref, history in state.items()
        if fold_stage_status(history.get("initial_screen") or ()) == "gate_passed"
    }
    return [
        str(member["company_ref"])
        for member in mission.get("universe") or []
        if member.get("company_ref") in passed
    ]


def claim_material(
    store: Any, company_ref: str, aspect: str, *, limit: int = MAX_CLAIM_ROWS
) -> list[dict[str, Any]]:
    """The canonical Claims for one aspect, importance first, bounded.

    ``canonical_only`` is the index's whole point: one copy of each fact, so a
    section does not cite the same quarter's revenue three times because three
    Claims assert it.
    """

    from .company_research_view import query_company_research

    rows = query_company_research(
        store, company_ref=company_ref, index_aspect=aspect,
        canonical_only=True, exclude_retired=True, limit=min(limit * 3, 1000),
    )
    from .claim_index_authority import IMPORTANCE_RANK

    rows.sort(key=lambda row: (
        IMPORTANCE_RANK.get(str(row.get("importance") or ""), 99),
        # Newest first inside a tier: an old filing and a new one are both
        # filings, and the new one is the one the file should rest on.
        str(row.get("as_of") or ""),
    ), reverse=False)
    material = []
    for row in rows[:limit]:
        material.append({
            "ref": row["claim_version_ref"],
            "text": row.get("normalized_statement") or "",
            "period": row.get("period"),
            "importance": row.get("importance"),
            # When this Claim entered the Ledger. Staleness is decided by this
            # and not by "material this section did not happen to cite": a
            # section that cites three of its ten Claims has not left seven
            # things undone, and treating it as stale would redraft it every
            # tick for ever and publish a duplicate each time.
            "created_at": row.get("created_at"),
            # Carried for P12f, which compares magnitudes rather than reading
            # them back out of prose. Ignored by the prompt builder, which
            # shows a statement and its tag and nothing else.
            "claim_kind": row.get("claim_kind"),
            "metric_or_aspect": row.get("metric_or_aspect"),
            "value": row.get("value"),
            "unit": row.get("unit"),
        })
    return material


def number_material(
    store: Any, company_ref: str, *, limit: int = MAX_NUMBER_ROWS
) -> list[dict[str, Any]]:
    """Figures the dossier may cite: filed statement lines and forecast cells.

    Both are refs a reader can open. A cell of the model input table is not:
    that projection is derived on demand and stored nowhere, so a ref into it
    would name something that does not exist. The filed line it was built from
    does exist, and it is what the number is actually evidence of.

    Each kind has its own quota. Filed lines are plentiful and forecast cells
    are few, so a single bound spent in filing order leaves no room for the
    quarters this system has an opinion about -- which are the ones a section
    on demand or margin most needs beside the history.

    Every row carries its ``value``, ``unit`` and period bounds as well as its
    text, because P12f pairs guidance against these rows and cannot parse a
    number back out of a sentence it wrote itself.
    """

    connection = store.connection
    forecast_quota = min(MAX_FORECAST_ROWS, max(0, limit // 3))
    filed = _statement_line_rows(connection, company_ref, limit=limit)
    cells = _forecast_cell_rows(connection, company_ref, limit=max(forecast_quota, 0))
    # The quota is a floor, not a ceiling: whatever one kind does not use, the
    # other may have.
    room_for_filed = limit - min(len(cells), forecast_quota)
    rows = filed[:max(room_for_filed, 0)]
    rows += cells[:limit - len(rows)]
    return rows


def _statement_line_rows(
    connection: Any, company_ref: str, *, limit: int
) -> list[dict[str, Any]]:
    if not table_exists(connection, "coverage_mission_statement_lines"):
        return []
    periods = [
        str(row["period_end"]) for row in connection.execute(
            "SELECT DISTINCT l.period_end AS period_end "
            "FROM coverage_mission_statement_lines l "
            "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
            "WHERE f.company_ref=? ORDER BY l.period_end DESC LIMIT ?",
            (company_ref, MAX_STATEMENT_PERIODS),
        ).fetchall()
    ]
    if not periods:
        return []
    placeholders = ",".join("?" * len(periods))
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in connection.execute(
        "SELECT l.line_id AS line_id, l.label AS label, l.concept AS concept, "
        "l.value AS value, l.unit AS unit, l.period_start AS period_start, "
        "l.period_end AS period_end, l.statement AS statement, "
        "f.accession AS accession "
        "FROM coverage_mission_statement_lines l "
        "JOIN coverage_mission_statement_filings f ON f.ingest_id=l.ingest_id "
        f"WHERE f.company_ref=? AND l.period_end IN ({placeholders}) "
        # The top of each statement, not its breakdowns: a dossier section
        # cites what the company reported, and the segment detail below it is
        # what the Claims are for. There is no level 0 in this ledger -- the
        # roots are levels one to three.
        "AND l.value IS NOT NULL AND l.level<=3 AND l.is_breakdown=0 "
        # Newest first, and the income statement before the cash flow before
        # the balance sheet. Without the statement order the bound is spent on
        # the tail of whichever statement happens to be filed last, and a
        # section about the business model is offered the closing cash balance
        # instead of revenue.
        "ORDER BY l.period_end DESC, CASE l.statement WHEN 'income' THEN 0 "
        "WHEN 'cash' THEN 1 ELSE 2 END, l.ordinal LIMIT ?",
        (company_ref, *periods, limit * 4),
    ).fetchall():
        # One row per measurement. The same quarter is filed twice -- once in
        # the 10-Q and again in the 10-K -- and a year-to-date figure sits
        # beside the quarter under the same label and the same period_end.
        # Both are why the span is in the key and in the text: "18.7bn for the
        # quarter" and "37.7bn year to date" are different facts, and a reader
        # who cannot tell them apart will read the second as a restatement of
        # the first.
        key = (str(row["concept"]), str(row["period_start"] or "-"),
               str(row["period_end"]))
        if key in seen:
            continue
        seen.add(key)
        span = (f"{row['period_start']}..{row['period_end']}"
                if row["period_start"] else f"{row['period_end']}")
        rows.append({
            "kind": "figure",
            "ref": f"statement-line:{row['line_id']}",
            "text": (f"{row['label']} ({row['concept']}) for {span} was "
                     f"{row['value']} {row['unit']}, as filed in "
                     f"{row['accession']}"),
            "period": span,
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "label": row["label"],
            "value": row["value"],
            "unit": row["unit"],
            "importance": "filing",
        })
        if len(rows) >= limit:
            break
    return rows


def _forecast_cell_rows(
    connection: Any, company_ref: str, *, limit: int
) -> list[dict[str, Any]]:
    if limit <= 0 or not table_exists(connection, "forecast_model_versions"):
        return []
    from .model_forecast_driver import ForecastModelAuthority, cell_ref
    row = connection.execute(
        "SELECT version_id FROM forecast_model_versions WHERE company_ref=? "
        "ORDER BY version_number DESC LIMIT 1", (company_ref,),
    ).fetchone()
    model = (None if row is None else ForecastModelAuthority.model(
        _ReadOnlyStoreView(connection), row["version_id"]))
    rows: list[dict[str, Any]] = []
    for line in (model or {}).get("results") or []:
        for cell in line.get("cells") or []:
            if cell.get("value") is None:
                continue
            period = cell["period"]
            end = str(period["end"])
            rows.append({
                "kind": "forecast_cell",
                "ref": cell_ref(str(line["ref"]), end, str(cell["kind"])),
                "text": (f"{line['label']} for the quarter ending {end} is "
                         f"{cell['value']} {line['unit']} ({cell['kind']})"),
                "period": (f"{period['start']}..{end}" if period.get("start") else end),
                "period_start": period.get("start"),
                "period_end": end,
                "label": line["label"],
                "value": cell["value"],
                "unit": line["unit"],
                # A filed actual is a filing; an estimate is ours, and the
                # importance ladder has no rung for our own opinion.
                "importance": "filing" if cell["kind"] == "actual" else "other",
            })
    # Newest first, so the quota buys the quarters nearest the present.
    rows.sort(key=lambda row: (str(row["period_end"]), row["ref"]), reverse=True)
    return rows[:limit]


def market_view_material(
    store: DaltonStore, company_ref: str, *, limit: int = MAX_CLAIM_ROWS
) -> list[dict[str, Any]]:
    """What is known about the market's view: sell-side Claims and price drivers.

    A deliberately narrow rule, stated rather than inferred: sell-side-graded
    Claims and Claims filed under ``history_of_price_drivers``. Consensus
    estimates (P11b) and sales-note material (S1) will arrive under the same
    two doors; until they do, most companies have nothing here and the variant
    view says so instead of imagining a street.
    """

    rows = claim_material(store, company_ref, "history_of_price_drivers", limit=limit)
    seen = {row["ref"] for row in rows}
    for aspect in SECTIONS:
        for row in claim_material(store, company_ref, aspect, limit=limit):
            if row.get("importance") == "sell_side" and row["ref"] not in seen:
                seen.add(row["ref"])
                rows.append(row)
    return rows[:limit]


def guidance_material(
    store: DaltonStore, company_ref: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """What P12f computes over: guidance statements and settled numbers."""

    guides = [
        {"ref": row["ref"], "text": row["text"], "period": row.get("period"),
         "label": row.get("text")}
        for row in claim_material(store, company_ref, "guidance_style")
    ]
    # A settled number can be a quantitative Claim as easily as a filed line:
    # "revenue grew 5.95% in the quarter" is exactly what a guidance range is
    # answered by, and on this Core it is the only settled growth figure that
    # exists. Claims come first because they carry the measure the guide was
    # about, and the index has already put filings ahead of news among them.
    actuals: list[dict[str, Any]] = []
    for aspect in SECTIONS:
        for row in claim_material(store, company_ref, aspect):
            if row.get("claim_kind") != "quantitative" or row.get("value") is None:
                continue
            actuals.append({
                "ref": row["ref"],
                "label": f"{row.get('metric_or_aspect') or ''} {row['text']}",
                "text": row["text"], "period": row.get("period"),
                "value": row.get("value"), "unit": row.get("unit"),
            })
    # The numbers, not the sentences about them. P12f compares magnitudes, so
    # it needs the value, the unit and the period bounds -- handing it prose
    # and a null value is handing it a row it must skip, which is how this
    # pairing came to be unable to settle a single event.
    actuals += [
        {
            "ref": row["ref"], "label": row.get("label") or row["text"],
            "text": row["text"], "period": row.get("period"),
            "period_start": row.get("period_start"),
            "period_end": row.get("period_end"),
            "value": row.get("value"), "unit": row.get("unit"),
        }
        for row in number_material(store, company_ref, limit=MAX_NUMBER_ROWS)
    ]
    return guides, actuals


def unresolved_refs(
    connection: Any, record: Mapping[str, Any], *, forecast_cells: set[str]
) -> list[dict[str, str]]:
    """Every ref the draft cites, checked against the authority that owns it.

    Q1's ``claim_refs_resolve`` resolves Claims; a dossier also cites filed
    statement lines and forecast cells, and a ref nobody can open is the one
    defect a citation-bearing document must not have.
    """

    kinds: dict[str, str] = {}
    for section in record.get("sections") or []:
        for row in section.get("sources") or []:
            kinds[row["ref"]] = row["kind"]
    for block in (record.get("industry_classification"), record.get("variant_view")):
        for row in (block or {}).get("sources") or []:
            kinds[row["ref"]] = row["kind"]
    missing: list[dict[str, str]] = []
    retired: set[str] = set()
    if table_exists(connection, "claim_retirement_decisions"):
        retired = {
            str(row[0]) for row in connection.execute(
                "SELECT claim_version_ref FROM claim_retirement_decisions "
                "WHERE decision='retired'").fetchall()
        }
    for ref, kind in sorted(kinds.items()):
        if kind == "claim":
            row = connection.execute(
                "SELECT 1 FROM claim_versions WHERE claim_version_id=?", (ref,)
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such claim version"})
            elif ref in retired:
                missing.append({"ref": ref, "reason": "the Claim was retired"})
        elif kind == "forecast_cell":
            if ref not in forecast_cells:
                missing.append({"ref": ref, "reason": "no such forecast cell"})
        elif ref.startswith("statement-line:"):
            row = connection.execute(
                "SELECT 1 FROM coverage_mission_statement_lines WHERE line_id=?",
                (ref.split(":", 1)[1],),
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such statement line"})
        elif ref.startswith("mission-document-figure:"):
            row = connection.execute(
                "SELECT 1 FROM coverage_mission_document_figures WHERE figure_id=?",
                (ref,),
            ).fetchone()
            if row is None:
                missing.append({"ref": ref, "reason": "no such document figure"})
        else:
            missing.append({"ref": ref, "reason": "unrecognised figure ref shape"})
    return missing


# ---------------------------------------------------------------------------
# what the run does
# ---------------------------------------------------------------------------


def fresh_evidence(
    blocks: Mapping[str, Any], prior: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """The rows this draft cites that the current version does not.

    ADR-0008's question, asked before a record is assembled rather than after,
    so that "there is nothing new here" is a status the run reports instead of
    a record it builds and the authority then refuses.
    """

    known = set() if prior is None else set(evidence_scope(prior))
    rows: dict[str, dict[str, Any]] = {}
    for block in blocks.values():
        for row in block.get("sources") or []:
            if row["ref"] not in known:
                rows.setdefault(row["ref"], row)
    return list(rows.values())


def units_citing(record: Mapping[str, Any], refs: set[str]) -> set[str]:
    """Which units of a record cite any of these refs."""

    out: set[str] = set()
    for section in record.get("sections") or []:
        if any(row["ref"] in refs for row in section.get("sources") or []):
            out.add(section["aspect"])
    for unit, key in ((CLASSIFICATION_UNIT, "industry_classification"),
                      (VARIANT_UNIT, "variant_view")):
        block = record.get(key) or {}
        if any(row["ref"] in refs for row in block.get("sources") or []):
            out.add(unit)
    return out


def unavailable_section(aspect: str, reason: str, structure: Sequence[str] = ()) -> dict[str, Any]:
    return {"aspect": aspect, "status": "unavailable", "reason": reason,
            "structure": list(structure), "slots": [], "sources": [], "gaps": [],
            "profile": None}


def undrafted_classification() -> dict[str, Any]:
    from .company_dossier import CLASSIFICATION_SLOTS

    return {
        "classification": "insufficient_evidence",
        "slots": [{"slot_id": slot, "unknown": "not drafted on this run"}
                  for slot in CLASSIFICATION_SLOTS],
        "sources": [], "gaps": [],
    }


def undrafted_variant(reason: str = "not_drafted_this_run") -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "market_view_available": False,
            "market_view_reason": None, "structure": [], "slots": [], "sources": [],
            "gaps": []}


def plan_units(
    *,
    store: DaltonStore,
    company_ref: str,
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per unit: can it be drafted, is it stale, and what would it be shown."""

    connection = store.connection
    prior_sections = {item["aspect"]: item for item in (prior or {}).get("sections") or []}
    plan: dict[str, Any] = {}
    numbers = number_material(store, company_ref)
    market = market_view_material(store, company_ref)
    for unit in UNITS:
        entry: dict[str, Any] = {"unit": unit, "status": "ready", "reason": None,
                                 "structure": [], "material": [], "new_refs": 0,
                                 "stale": True}
        try:
            structure = unit_slots(
                unit, constitution=constitution, policy=policy,
                market_view_available=bool(market),
            )
        except DossierStructureUnmapped as exc:
            entry.update({"status": "unavailable", "reason": "causal_chain_unmapped",
                          "detail": str(exc)})
            plan[unit] = entry
            continue
        entry["structure"] = list(structure)
        # A section is made of Claims. Figures accompany them -- a filed
        # number is what a sentence about the business rests on -- but they do
        # not by themselves make a section draftable: a business-model section
        # written from the tail of a cash-flow statement is what "assembled by
        # grouping Claims" was supposed to stop.
        if unit in SECTIONS:
            claims = claim_material(store, company_ref, unit)
            material = (claims + [row for row in numbers
                                  if unit in FIGURE_SECTIONS]) if claims else []
        elif unit == CLASSIFICATION_UNIT:
            material = [row for aspect in ("business_model", "segments_and_mix",
                                           "demand_drivers", "supply_and_cost")
                        for row in claim_material(store, company_ref, aspect, limit=10)]
        else:
            material = (market + [row for row in claim_material(
                store, company_ref, "competitive_position", limit=10)]) if market else []
        if not material:
            reason = "no_canonical_claims"
            if unit == "catalyst_calendar" and not table_exists(
                connection, "catalyst_calendar_versions"
            ):
                reason = "no_catalyst_calendar_authority"
            elif unit == "history_of_price_drivers" and not table_exists(
                connection, "market_price_series_versions"
            ):
                reason = "no_market_data"
            entry.update({"status": "unavailable", "reason": reason})
            plan[unit] = entry
            continue
        entry["material"] = material
        held = prior_sections.get(unit)
        if unit == CLASSIFICATION_UNIT:
            held = (prior or {}).get("industry_classification")
        elif unit == VARIANT_UNIT:
            held = (prior or {}).get("variant_view")
        cited = {row["ref"] for row in (held or {}).get("sources") or []}
        entry["new_refs"] = len({row["ref"] for row in material} - cited)
        drafted_before = held is not None and held.get("status") != "unavailable"
        # When *this* unit was last written, not when the chain last moved.
        # Against the chain head, a unit nobody has ever drafted looks current
        # the moment any other unit is published, and with three units a tick
        # over twelve units most of the file would never be written.
        written_at = str(((prior or {}).get("drafted_at") or {}).get(unit) or "")
        entry["last_drafted"] = written_at or None
        newest = max((str(row.get("created_at") or "") for row in material),
                     default="")
        entry["stale"] = (
            not drafted_before or not written_at or newest > written_at)
        plan[unit] = entry
    return plan


def build_dossier_input(
    *, unit: str, structure: Sequence[Mapping[str, str]],
    material: Sequence[Mapping[str, Any]], company: Mapping[str, Any],
    mission: Mapping[str, Any], constitution: Mapping[str, Any],
    policy: Mapping[str, Any], prior_body: str = "", profile_table: str = "",
    market_view_available: bool = True, classification: str | None = None,
) -> dict[str, Any]:
    """Freeze the exact call input plus the governance bindings authorising it."""

    prompt = build_unit_prompt(
        unit=unit, structure=structure, material=material, company=company,
        prior_body=prior_body, profile_table=profile_table,
        market_view_available=market_view_available, classification=classification)
    return {
        "unit": unit,
        "prompt_sha": dossier_input_fingerprint({"prompt": prompt}),
        "mission": {"ref": mission["id"], "hash": mission["content_hash"]},
        "constitution": {"ref": constitution["id"], "hash": constitution["content_hash"]},
        "policy": {"ref": policy["policy_ref"], "hash": policy_hash(policy)},
    }


class _ReadOnlyStoreView:
    """Narrow adapter for existing query readers; it exposes only connection."""

    def __init__(self, connection: Any):
        self.connection = connection

    def claim_index_snapshot(self, *, created_at: str | None = None) -> dict[str, Any]:
        # This DaltonStore reader only consumes ``connection``. Calling the
        # unbound method avoids constructing an authority (and therefore DDL).
        return DaltonStore.claim_index_snapshot(
            self, created_at=created_at, reuse_read_transaction=True)


def _json_row(connection: Any, query: str, params: Sequence[Any]) -> dict[str, Any] | None:
    row = connection.execute(query, tuple(params)).fetchone()
    return None if row is None else json.loads(row["record_json"])


def reconstruct_dossier_input(
    connection: Any, record: Mapping[str, Any], current_mission: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Rebuild current producer input using SELECT-only access and a fixed predecessor."""

    prior_ref = record.get("prior_version_ref")
    prior = None if prior_ref is None else _json_row(
        connection, "SELECT record_json FROM company_dossier_versions WHERE version_id=?",
        (prior_ref,),
    )
    binding = current_mission["bindings"]["constitution_version"]
    constitution = _json_row(
        connection,
        "SELECT record_json FROM research_constitution_versions WHERE constitution_version_id=?",
        (binding["ref"],),
    )
    if constitution is None or constitution.get("content_hash") != binding["hash"]:
        raise ValueError("current mission constitution binding does not resolve exactly")
    company_ref = str(record["company_ref"])
    view = _ReadOnlyStoreView(connection)
    plan = plan_units(store=view, company_ref=company_ref, constitution=constitution,
                      policy=policy, prior=prior)
    guides, actuals = guidance_material(view, company_ref)
    profile = build_profile(company_ref=company_ref, guides=guides, actuals=actuals)
    company = {"company_ref": company_ref, "ticker": next(
        (member.get("ticker") for member in current_mission.get("universe") or []
         if member.get("company_ref") == company_ref), None)}
    prior_classification = str(
        ((prior or {}).get("industry_classification") or {}).get("classification") or "") or None
    current_classification = str(
        (record.get("industry_classification") or {}).get("classification") or "") or None
    profile_table = render_profile_table(profile)
    rebuilt = {}
    prior_sections = {item["aspect"]: item for item in (prior or {}).get("sections") or []}
    for unit in UNITS:
        entry = plan[unit]
        held = prior_sections.get(unit)
        material = material_rows(
            [row for row in entry.get("material", []) if "kind" not in row],
            [row for row in entry.get("material", []) if "kind" in row])
        rebuilt[unit] = build_dossier_input(
            unit=unit, structure=entry.get("structure", []), material=material,
            company=company, mission=current_mission, constitution=constitution,
            policy=policy, prior_body="" if held is None else section_body(held),
            profile_table=profile_table if unit == "guidance_style" else "",
            market_view_available=any(
                slot["slot_id"] == "market_view" for slot in entry.get("structure", [])),
            classification=(prior_classification if unit == CLASSIFICATION_UNIT
                            else current_classification))
    return rebuilt


def dossier_freshness(connection: Any, record: Mapping[str, Any],
                      current_mission: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    stored = record.get("input_fingerprints")
    if stored is None:
        return "unknown"
    rebuilt = reconstruct_dossier_input(connection, record, current_mission, policy)
    current = {unit: dossier_input_fingerprint(value) for unit, value in rebuilt.items()}
    for unit, fingerprint in stored.items():
        if fingerprint is not None and current[unit] != fingerprint:
            return "stale"
    # A draftable section skipped by the per-run quota is incomplete, even
    # though every section actually produced this run used fresh input.
    binding = current_mission["bindings"]["constitution_version"]
    constitution = _json_row(connection,
        "SELECT record_json FROM research_constitution_versions WHERE constitution_version_id=?",
        (binding["ref"],))
    plan = plan_units(store=_ReadOnlyStoreView(connection),
                      company_ref=record["company_ref"], constitution=constitution,
                      policy=policy, prior=record)
    return "unknown" if any(
        stored[unit] is None and (_unit_was_drafted(record, unit)
                                 or plan[unit]["status"] == "ready")
        for unit in UNITS) else "fresh"


def dossier_input_fingerprint(value: Mapping[str, Any]) -> str:
    from .store import content_hash
    return content_hash(dict(value))


def stale_units(
    plan: Mapping[str, Any], *, limit: int = MAX_UNITS_PER_RUN,
    revise: Sequence[str] = (),
) -> list[str]:
    """The units to draft this tick: never drafted first, then most new evidence.

    "Stalest" is a fact about the file, not about the clock: a unit nobody has
    drafted is staler than one drafted last week, and a unit with twelve new
    Claims is staler than one with one. A unit with no new evidence is not
    drafted at all -- redrafting it would produce a version ADR-0008 refuses.

    ``revise`` is the other door: a person asking for one part to be written
    again. It overrides staleness and nothing else -- a unit whose material is
    missing is still unavailable, and the version it produces still has to cite
    something the current one does not.
    """

    order = {unit: index for index, unit in enumerate(UNITS)}
    asked = set(revise)
    ready = [entry for entry in plan.values()
             if entry["status"] == "ready"
             and (entry["unit"] in asked
                  or (entry.get("stale") and entry["new_refs"] > 0))]
    ready.sort(key=lambda entry: (
        # Classification supplies the demand template, even when another
        # section has more new references in this bounded batch.
        0 if entry["unit"] == CLASSIFICATION_UNIT else 1,
        0 if entry.get("last_drafted") is None else 1,
        -int(entry["new_refs"]),
        order[entry["unit"]],
    ))
    return [entry["unit"] for entry in ready[:limit]]


def run_dossier(
    *,
    state_dir: Path,
    model_config_path: Path | None,
    summary_dir: Path,
    scheduler_db: Path | None = None,
    policy_path: Path | None = None,
    company_ref: str | None = None,
    change_reason: str = "evidence_thicker",
    revise_units: Sequence[str] = (),
    max_units: int | None = None,
    dry_run: bool = False,
    verifier_model_config_path: Path | None = None,
    model_factory: Callable[..., Any] | None = None,
    verifier_model_factory: Callable[..., Any] | None = None,
    family_resolver: Callable[[str | None], str | None] | None = None,
) -> dict[str, Any]:
    state_dir = Path(state_dir).expanduser().resolve()
    summary_dir = Path(summary_dir)
    summary_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "created_at": _now(),
        "status": "failed",
        "dossier_status": None,
        "company_ref": company_ref,
        "write_scope": None,
        "units_planned": {},
        "units_drafted": [],
        "refused": [],
        "verification": None,
        "rubric": None,
        "output_rubric_findings": [],
        "dropped_units": [],
        "version_ref": None,
        "version_status": None,
        "new_refs": 0,
        "cost_micros": 0,
        "failure_reason": None,
        "formal_authority_writes": 0,
        "failed_model_traces": [],
    }
    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        missions = CoverageMissionAuthority(store)
        pointer = store.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer "
            "ORDER BY mission_ref LIMIT 1"
        ).fetchone()
        if pointer is None:
            summary.update({"status": "idle", "dossier_status": "no_mission"})
            return summary
        mission = missions.mission(pointer["mission_version_id"])
        scope = granted_scope(mission)
        if scope is None:
            summary.update({
                "status": "held", "dossier_status": "not_authorized",
                "failure_reason": (
                    f"任务 {mission['id']} 还没有授予 {WRITE_SCOPE} 写入范围；"
                    "在授权前不起草，免得白花模型调用"),
            })
            return summary
        summary["write_scope"] = scope
        actor_ref = mission["autonomy"]["automation_principal"]

        from .claim_index_authority import table_exists as index_exists

        if not index_exists(store.connection):
            summary.update({
                "status": "held", "dossier_status": "no_claim_index",
                "failure_reason": ("this Core holds no Claim index; a dossier "
                                   "section is 'the canonical Claims for one aspect' "
                                   "and without the index there is no such set"),
            })
            return summary

        candidates = screened_companies(missions, mission)
        if company_ref is not None:
            candidates = [company_ref] if company_ref in candidates else []
        if not candidates:
            summary.update({"status": "idle", "dossier_status": "no_screened_company",
                            "failure_reason": "no company in this mission has passed "
                                              "its Initial Screen"})
            return summary

        from .research_constitution import ResearchConstitutionAuthority

        binding = mission["bindings"]["constitution_version"]
        constitution = ResearchConstitutionAuthority(store).constitution(binding["ref"])
        if constitution["content_hash"] != binding["hash"]:
            summary.update({"status": "failed", "dossier_status": "binding_drift",
                            "failure_reason": "the mission's constitution binding drifted"})
            return summary
        try:
            policy = load_policy(policy_path)
        except (OSError, ValueError) as exc:
            # The policy is a deploy artefact and the default path only exists
            # in a source checkout. An installation without it is held rather
            # than crashed: the structure of two sections is a human decision,
            # and its absence is a wiring gap, not a bad run.
            summary.update({
                "status": "held", "dossier_status": "no_policy",
                "failure_reason": f"the dossier policy could not be read: {exc}"})
            return summary
        authority = CompanyDossierAuthority(store)
        config: Mapping[str, Any] = {}
        if model_config_path is not None:
            config = json.loads(Path(model_config_path).expanduser().read_text(encoding="utf-8"))
        run_budget = resolve_run_budget(config, "dossier", defaults={
            "max_cost_usd": MAX_RUN_COST_USD, "max_units": MAX_UNITS_PER_RUN})
        max_units = int(max_units if max_units is not None
                        else run_budget.get("max_units", MAX_UNITS_PER_RUN))
        run_cost_micros = int(float(run_budget["max_cost_usd"]) * 1_000_000)

        chosen = None
        plan: dict[str, Any] = {}
        prior = None
        examined = candidates[0]
        for candidate in candidates:
            examined = candidate
            prior = authority.latest(candidate)
            plan = plan_units(store=store, company_ref=candidate,
                              constitution=constitution, policy=policy, prior=prior)
            if stale_units(plan, limit=max_units, revise=revise_units):
                chosen = candidate
                break
        def planned(entries: Mapping[str, Any]) -> dict[str, Any]:
            return {
                unit: {"status": entry["status"], "reason": entry["reason"],
                       "new_refs": entry["new_refs"], "stale": entry["stale"],
                       "slots": len(entry["structure"])}
                for unit, entry in entries.items()
            }

        if chosen is None:
            # An idle run still says what it looked at. "Nothing to do" without
            # the plan behind it is the summary an operator cannot act on.
            summary.update({
                "status": "idle", "dossier_status": "nothing_new",
                "company_ref": examined, "units_planned": planned(plan),
                "failure_reason": "no company has new canonical Claims for "
                                  "any part of its file"})
            return summary
        summary["company_ref"] = chosen
        summary["units_planned"] = planned(plan)
        wanted = stale_units(plan, limit=max_units, revise=revise_units)
        if dry_run:
            summary.update({"status": "succeeded", "dossier_status": "dry_run",
                            "units_drafted": wanted})
            return summary
        if model_config_path is None:
            summary.update({"status": "succeeded", "dossier_status": "gated",
                            "failure_reason": "no model configured",
                            "units_drafted": []})
            return summary

        def build(settings: Mapping[str, Any]) -> Any:
            return CockpitModel(
                settings,
                scheduler_db=str(scheduler_db or (state_dir / "scheduler.sqlite")),
                max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)

        factory = model_factory or (lambda: build(config))
        verifier_factory = verifier_model_factory
        if verifier_factory is None and verifier_model_config_path is not None:
            verifier_config = json.loads(
                Path(verifier_model_config_path).expanduser().read_text(encoding="utf-8"))
            verifier_factory = lambda: build(verifier_config)  # noqa: E731
        if verifier_factory is None:
            # Held before a single drafting call. One configuration routes both
            # calls the same way, so the verifier would land on the family that
            # drafted and the run would refuse to publish after paying for
            # twelve calls. The cheapest correct answer is to say so first.
            summary.update({
                "status": "held", "dossier_status": "no_verifier",
                "failure_reason": ("no separate verifier model configuration is "
                                   "wired; a dossier is published only on a verdict "
                                   "from a different model family (D2)")})
            return summary
        model = factory()
        producer_reserve = int(float(getattr(model, "budget_for", lambda p: {
            "max_cost_usd": MAX_COST_USD})("dossier")["max_cost_usd"]) * 1_000_000)
        verifier_budget = resolve_call_budget(
            verifier_config if verifier_model_config_path is not None else {},
            "dossier_verifier", defaults={"max_input_tokens": MAX_INPUT_TOKENS,
                "max_output_tokens": MAX_OUTPUT_TOKENS, "max_cost_usd": MAX_COST_USD,
                "timeout_seconds": TIMEOUT_SECONDS})
        verifier_reserve = int(float(verifier_budget["max_cost_usd"]) * 1_000_000)
        company = {"company_ref": chosen, "ticker": next(
            (member.get("ticker") for member in mission["universe"]
             if member.get("company_ref") == chosen), None)}

        guides, actuals = guidance_material(store, chosen)
        profile = build_profile(company_ref=chosen, guides=guides, actuals=actuals)
        profile_table = render_profile_table(profile)
        held_classification = str(
            ((prior or {}).get("industry_classification") or {}).get("classification")
            or "") or None
        blocks: dict[str, Any] = {}
        draft_routes: list[str | None] = []
        input_fingerprints = {unit: None for unit in UNITS}
        spent = 0
        run_bound_blocked = False
        attempted_outcomes: list[str] = []
        prior_sections = {item["aspect"]: item
                          for item in (prior or {}).get("sections") or []}
        for unit in wanted:
            if spent + producer_reserve + verifier_reserve > run_cost_micros:
                run_bound_blocked = True
                summary["refused"].append({"unit": unit, "reason": "run cost bound reached"})
                continue
            entry = plan[unit]
            held = prior_sections.get(unit)
            # A claim row has no ``kind``; a figure or forecast row does. That
            # is the whole distinction the two tables in the prompt make.
            material = material_rows(
                [row for row in entry["material"] if "kind" not in row],
                [row for row in entry["material"] if "kind" in row],
            )
            actual_classification = (blocks.get(CLASSIFICATION_UNIT) or {}).get(
                "classification", held_classification)
            prior_body = "" if held is None else section_body(held)
            unit_profile_table = profile_table if unit == "guidance_style" else ""
            market_view_available = any(
                slot["slot_id"] == "market_view" for slot in entry["structure"])
            frozen_input = build_dossier_input(
                unit=unit, structure=entry["structure"], material=material,
                company=company, mission=mission, constitution=constitution,
                policy=policy, prior_body=prior_body,
                profile_table=unit_profile_table,
                market_view_available=market_view_available,
                classification=actual_classification,
            )
            outcome = draft_unit(
                model, unit=unit, structure=entry["structure"], material=material,
                company=company, mission=mission,
                # Use the classification drafted earlier in this same run;
                # a first dossier should not need another tick to choose its frame.
                classification=actual_classification,
                prior_body=prior_body,
                profile=profile if unit == "guidance_style" else None,
                profile_table=unit_profile_table,
                market_view_available=market_view_available,
            )
            if outcome.get("failure_trace") is not None:
                summary["failed_model_traces"].append(outcome["failure_trace"])
            spent += int((outcome.get("model") or {}).get("cost_micros") or 0)
            attempted_outcomes.append(str(outcome.get("status") or ""))
            if outcome["status"] != "drafted":
                summary["refused"].append({"unit": unit, "reason": outcome["reason"]})
                continue
            blocks[unit] = outcome["block"]
            input_fingerprints[unit] = dossier_input_fingerprint(frozen_input)
            draft_routes.append((outcome.get("model") or {}).get("route_decision_ref"))
        summary["cost_micros"] = spent
        summary["units_drafted"] = sorted(blocks)
        if not blocks:
            model_refusals = [item for item in summary["refused"]
                              if item.get("reason") != "run cost bound reached"]
            if model_refusals:
                reasons = [str(item.get("reason") or "draft contract refused")
                           for item in model_refusals[:3]]
                summary.update({
                    "status": "failed", "dossier_status": (
                        "rubric_refused"
                        if attempted_outcomes and all(
                            status == "refused" for status in attempted_outcomes)
                        else "nothing_drafted"),
                    "failure_reason": ("all attempted dossier units were refused: "
                                       + "; ".join(reasons))[:500],
                })
            else:
                summary.update({"status": "succeeded", "dossier_status": (
                    "unverified" if run_bound_blocked else "nothing_drafted")})
            return summary

        resolve = family_resolver or router_family_resolver(config)
        # D2, decided before anyone pays for it. A drafting family that cannot
        # be read back from its route decision makes every possible verdict
        # unverifiable, so the second call is not worth making.
        early = independence_precheck(draft_routes=draft_routes, resolve=resolve)
        if early is not None:
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "not attempted", "independent": False,
                "independence_reason": early["reason"],
                "verifier_family": None,
            }
            summary.update({"status": "succeeded",
                            "dossier_status": "not_independent"})
            return summary
        if spent + verifier_reserve > run_cost_micros:
            # The verification is not optional, so a run that cannot afford it
            # publishes nothing rather than publishing unverified.
            summary["verification"] = {
                "status": "skipped", "verdict": None, "findings": [],
                "reason": "the run's cost bound leaves no room for the verifier",
                "independent": False, "independence_reason": None,
                "verifier_family": None,
            }
            summary.update({"status": "succeeded", "dossier_status": "unverified"})
            return summary
        verifier = verifier_factory()
        verdict = verify(
            verifier, blocks, company=company, mission=mission,
            producer_route_decision_refs=draft_routes,
        )
        if verdict.get("failure_trace") is not None:
            summary["failed_model_traces"].append(verdict["failure_trace"])
        spent += int((verdict.get("model") or {}).get("cost_micros") or 0)
        summary["cost_micros"] = spent
        check = independence(
            draft_routes=draft_routes,
            verifier_route=(verdict.get("model") or {}).get("route_decision_ref"),
            resolve=resolve,
        )
        summary["verification"] = {
            "status": verdict.get("status"), "verdict": verdict.get("verdict"),
            "findings": verdict.get("findings") or [],
            "reason": verdict.get("reason"),
            "independent": check["independent"],
            "independence_reason": check["reason"],
            "verifier_family": check["verifier_family"],
        }
        if verdict.get("status") != "verified" or verdict.get("verdict") != "pass":
            reason = verdict.get("reason")
            if not reason and verdict.get("findings"):
                reason = json.dumps(verdict["findings"][:3], ensure_ascii=False)
            summary.update({
                "status": "failed", "dossier_status": "verification_failed",
                "failure_reason": ("dossier verification did not pass: "
                                   + str(reason or verdict.get("verdict") or "refused"))[:500],
            })
            return summary
        if not check["independent"]:
            summary.update({"status": "succeeded", "dossier_status": "not_independent"})
            return summary

        # ADR-0008, asked before a record exists. A draft that cites nothing
        # the current version does not is not a version, and saying so here
        # costs nothing; assembling one and having the authority refuse it
        # would leave the summary describing a record nobody can read.
        fresh = fresh_evidence(blocks, prior)
        if not fresh:
            summary.update({"status": "succeeded", "dossier_status": "no_new_evidence",
                            "failure_reason": ("this draft cites nothing the current "
                                               "version does not")})
            return summary
        stamped = _now()
        record = assemble(
            company_ref=chosen, blocks=blocks, plan=plan, prior=prior,
            profile=profile, constitution=constitution, policy=policy,
            mission=mission, actor_ref=actor_ref, change_reason=change_reason,
            drafted_at=stamped, evidence_refs=fresh,
        )
        forecast_cells = {row["ref"] for row in number_material(store, chosen)
                          if row["kind"] == "forecast_cell"}
        missing = unresolved_refs(store.connection, record, forecast_cells=forecast_cells)
        if missing:
            # A ref that stopped resolving is a defect in whichever part cites
            # it. In a part drafted just now that is a bad draft and the run is
            # refused. In a part carried forward from an earlier version -- a
            # Claim retired since it was written -- refusing would freeze the
            # whole chain on one stale section for ever, so that section drops
            # to unavailable and the lane redrafts it. The old version keeps
            # what it said; nothing is edited.
            drafted_refs = {row["ref"] for block in blocks.values()
                            for row in block.get("sources") or []}
            broken = {item["ref"] for item in missing}
            if broken & drafted_refs:
                summary.update({
                    "status": "succeeded", "dossier_status": "unresolvable_refs",
                    "failure_reason": json.dumps(
                        [item for item in missing if item["ref"] in drafted_refs][:5],
                        ensure_ascii=False)})
                return summary
            dropped = units_citing(record, broken) - set(blocks)
            summary["dropped_units"] = sorted(dropped)
            record = assemble(
                company_ref=chosen, blocks=blocks, plan=plan, prior=prior,
                profile=profile, constitution=constitution, policy=policy,
                mission=mission, actor_ref=actor_ref, change_reason=change_reason,
                drafted_at=stamped, evidence_refs=fresh, drop_units=dropped,
            )
            still = unresolved_refs(store.connection, record,
                                    forecast_cells=forecast_cells)
            if still:
                summary.update({
                    "status": "succeeded", "dossier_status": "unresolvable_refs",
                    "failure_reason": json.dumps(still[:5], ensure_ascii=False)})
                return summary
        record["input_fingerprints"] = input_fingerprints
        gate = rubric_gate(store.connection, record, prior=prior)
        summary["rubric"] = gate["summary"]
        if gate["failed"]:
            summary.update({"status": "succeeded", "dossier_status": "rubric_refused",
                            "failure_reason": "hard checks failed: "
                                              + ", ".join(gate["failed"])})
            return summary
        findings = output_rubric_findings(record, constitution=constitution,
                                          policy=policy, prior=prior)
        summary["output_rubric_findings"] = findings
        if findings:
            summary.update({"status": "succeeded", "dossier_status": "constitution_refused",
                            "failure_reason": "the Constitution's output_rubric was not "
                                              "satisfied"})
            return summary
        summary["new_refs"] = len(new_refs(record, prior))
        published = authority.publish(record)
        summary.update({
            "status": "succeeded",
            "dossier_status": ("published" if published["status"] == "fresh"
                               else "duplicate"),
            "version_ref": published["id"],
            "version_status": published["status"],
            "duplicate_reason": published.get("duplicate_reason"),
            "input_freshness": dossier_freshness(
                store.connection, published, mission, policy),
            **summarise_blocks(blocks),
        })
        return summary
    except CompanyDossierError as exc:
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        summary["status"] = "failed"
        return summary
    except Exception as exc:  # unexpected: record for the parent, then surface
        summary["failure_reason"] = f"unexpected {type(exc).__name__}: {exc}"
        raise
    finally:
        _write_owner_only(summary_dir / "summary.json", summary)
        store.close()


def assemble(
    *,
    company_ref: str,
    blocks: Mapping[str, Any],
    plan: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
    profile: Mapping[str, Any] | None,
    constitution: Mapping[str, Any],
    policy: Mapping[str, Any],
    mission: Mapping[str, Any],
    actor_ref: str,
    change_reason: str,
    drafted_at: str,
    evidence_refs: Sequence[Mapping[str, Any]],
    drop_units: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    """Freshly drafted parts, carried-forward parts, and the bindings."""

    from .company_dossier import SOURCE_VERSION_KEY, causal_chain_hash
    from .research_quality_rubrics import rubric as get_rubric

    prior_sections = {item["aspect"]: item for item in (prior or {}).get("sections") or []}
    sections = []
    for aspect in SECTIONS:
        if aspect in drop_units and aspect not in blocks:
            sections.append(unavailable_section(aspect, "refused_by_verification"))
            continue
        if aspect in blocks:
            section = dict(blocks[aspect])
            if aspect == "guidance_style" and profile is not None:
                section["profile"] = profile
            sections.append(section)
            continue
        held = prior_sections.get(aspect)
        if held is not None:
            sections.append(dict(held))
            continue
        entry = plan.get(aspect) or {}
        sections.append(unavailable_section(
            aspect,
            entry.get("reason") or "not_drafted_this_run",
            [slot["slot_id"] for slot in entry.get("structure") or []],
        ))
    classification = blocks.get(CLASSIFICATION_UNIT) or (
        undrafted_classification() if CLASSIFICATION_UNIT in drop_units
        else (prior or {}).get("industry_classification") or undrafted_classification())
    variant = blocks.get(VARIANT_UNIT)
    if variant is not None:
        pass
    elif VARIANT_UNIT in drop_units:
        variant = undrafted_variant("refused_by_verification")
    else:
        variant = (prior or {}).get("variant_view") or undrafted_variant(
            (plan.get(VARIANT_UNIT) or {}).get("reason") or "not_drafted_this_run")
    chain = list((constitution.get("method") or {}).get("causal_chain") or [])
    rubric = get_rubric("company_dossier")
    return {
        SOURCE_VERSION_KEY: None if prior is None else prior["id"],
        "company_ref": company_ref,
        "drafted_at": {
            **((prior or {}).get("drafted_at") or {}),
            **{unit: drafted_at for unit in blocks},
        },
        "sections": sections,
        "industry_classification": classification,
        "variant_view": variant,
        "bindings": {
            "constitution_version": {"ref": constitution["id"],
                                     "hash": constitution["content_hash"]},
            "playbook_version": {
                "ref": mission["bindings"]["playbook_version"]["ref"],
                "hash": mission["bindings"]["playbook_version"]["hash"]},
            "mission_version_ref": mission["id"],
            "policy_ref": policy["policy_ref"],
            "policy_hash": policy_hash(policy),
            "causal_chain_hash": causal_chain_hash(chain) if chain else None,
            "rubric_ref": rubric.rubric_ref,
            "rubric_hash": rubric.content_hash,
        },
        "actor_ref": actor_ref,
        "change_reason": change_reason,
        "evidence_refs": [dict(row) for row in evidence_refs],
        "decision": None,
    }


def rubric_gate(
    connection: Any, record: Mapping[str, Any], *, prior: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Q1's deterministic layer, run on the draft before it can be a version.

    One override, and it is about a difference in what the two layers can see.
    Q1's ``new_version_cites_new_refs`` reads Claim refs, because that is what
    every artefact it grades cites. A dossier also rests on filed statement
    lines and forecast cells, and a version whose new evidence is a newly filed
    line has learned something -- ADR-0008 says so, and ``new_refs`` counts all
    three kinds. Where the two disagree the broader one wins, and the summary
    records that it did.
    """

    from .company_dossier import body_hash
    from .research_quality_rubrics import rubric as get_rubric
    from .research_quality_score import run_deterministic

    digest = body_hash(record)
    art = dossier_artefact(
        {**dict(record), "id": f"company-dossier-draft:{digest[:32]}",
         "content_hash": digest, "dossier_ref": record.get("company_ref")},
        prior=prior,
    )
    result = run_deterministic(art, get_rubric("company_dossier"), core=connection)
    failed = [name for name in result["failed_checks"] if name in HARD_CHECKS]
    overridden = []
    if "new_version_cites_new_refs" in failed and new_refs(record, prior):
        failed = [name for name in failed if name != "new_version_cites_new_refs"]
        overridden.append("new_version_cites_new_refs")
    return {
        "failed": failed,
        "summary": {
            "passed": result["passed"],
            "failed_checks": result["failed_checks"],
            "hard_failed": failed,
            "overridden_checks": overridden,
            "checks": {item["check"]: {"status": item["status"], "count": item["count"]}
                       for item in result["checks"]},
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--verifier-model-config", type=Path,
                        help="the configuration the independent verifier runs on. "
                             "Without it the run is held: one configuration routes "
                             "both calls the same way (D2).")
    parser.add_argument("--summary-dir", type=Path, help="defaults to the state dir")
    parser.add_argument("--scheduler-db", type=Path)
    parser.add_argument("--dossier-policy", type=Path,
                        help="defaults to deploy/phase9/p12a-dossier-policy-v1.json")
    parser.add_argument("--company-ref", help="draft this company rather than choosing")
    parser.add_argument("--change-reason", default="evidence_thicker")
    parser.add_argument("--revise", action="append", default=[], dest="revise_units",
                        help="draft this part again even if nothing new arrived; "
                             "repeatable. The version still has to cite something "
                             "the current one does not.")
    parser.add_argument("--max-units", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="plan and stop; no model call and no write")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_dossier(
        state_dir=args.state_dir, model_config_path=args.model_config,
        verifier_model_config_path=args.verifier_model_config,
        summary_dir=args.summary_dir if args.summary_dir is not None else args.state_dir,
        scheduler_db=args.scheduler_db, policy_path=args.dossier_policy,
        company_ref=args.company_ref, change_reason=args.change_reason,
        revise_units=tuple(args.revise_units), max_units=args.max_units,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=1))
    return 0 if summary["status"] in ("succeeded", "idle", "held") else 1


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())


__all__ = [
    "HARD_CHECKS",
    "assemble",
    "build_dossier_input",
    "build_parser",
    "claim_material",
    "dossier_input_fingerprint",
    "dossier_freshness",
    "granted_scope",
    "guidance_material",
    "main",
    "market_view_material",
    "number_material",
    "plan_units",
    "reconstruct_dossier_input",
    "rubric_gate",
    "run_dossier",
    "screened_companies",
    "stale_units",
    "table_exists",
    "unavailable_section",
    "undrafted_classification",
    "undrafted_variant",
    "unresolved_refs",
]
