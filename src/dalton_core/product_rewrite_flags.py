"""Products whose current version rests on retired Claims need a rewrite.

P0 (research artifact quality review, 2026-10-09; ADR-0017).  When Claims are
retired -- above all under ``issuer_misattribution`` (an MSFT Claim cut from
Oracle's model pages) -- the products that cite them keep showing the retired
citation as retired (that display stays).  What was missing is the
consequence: the reasoning built on them has to be rewritten, not footnoted.

Every product kind is read (``PRODUCT_TABLES``): mission deliverables (initial
screen, earnings preview, event note), dossier, debate map, scenarios, deep
insight gate, industry framework, event judgements, zero-base review, thesis.
A section needs a rewrite at once when it cites a Claim retired for an
``IMMEDIATE_REASONS`` reason, and for any other reason once fewer than
``VALID_CITATION_SHARE`` of the Claims it cites still stand.

Owner decision (research capability diagnosis 2026-10-09, item 4): redrafts
are batched, and a rewrite needs replacement material first.  So nothing here
redrafts.  A ``needs_rewrite`` flag is an append-only record naming the exact
product version, the affected sections, the retired Claims and the material
that must arrive (``requires_material``, read by the material acquisition
service); the product lanes read :func:`needs_rewrite` when they plan their
next batched redraft and rewrite once that material has arrived.  A flag is
open while the version it names is still current: the lane's next version
answers it, and nothing has to be closed by hand.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import DaltonStore, authorization_flag, authorized_flag, content_hash

SCHEMA_VERSION = "0.2"
_SCHEMA_PATH = Path(__file__).with_name("product_rewrite_flags_schema.sql")
RULE_REF = "product-rewrite:cites-retired-claim:v2"
#: A retirement for one of these reasons makes a section need a rewrite on its
#: own: the reasoning built on another issuer's table is not the subject's.
IMMEDIATE_REASONS = frozenset({"issuer_misattribution"})
#: Kept for callers of v1: every retirement reason is now checked.
REWRITE_REASONS = IMMEDIATE_REASONS
#: Any other retirement reason: a section needs a rewrite once fewer than
#: this share of the Claims it cites still stand.
VALID_CITATION_SHARE = 0.5
#: Severity, most urgent first.  ``immediate``: an ``IMMEDIATE_REASONS``
#: retirement.  ``high``: not one Claim the section cites still stands (an
#: industry framework whose every citation is retired).  ``threshold``: fewer
#: than ``VALID_CITATION_SHARE`` still stand.
SEVERITIES: tuple[str, ...] = ("immediate", "high", "threshold")
#: How a paragraph citing a retired Claim reads until its product is
#: rewritten: a historical citation, not current support (batch an).
HISTORICAL_CITATION_LABEL = "历史引用"
HISTORICAL_CITATION_NOTE = "这段引用的结论已退休，仅作历史引用保留，不再作为当前依据；待重写后替换"
_CLAIM_RE = re.compile(r"claim-version:[0-9a-f]{64}")

#: product kind -> (table, identity column, subject column[, order column,
#: record column]).  The current version of each identity is the row with the
#: highest order value (``version_number`` unless said otherwise).  A table a
#: Core does not have is skipped.
PRODUCT_TABLES: Mapping[str, tuple[Any, ...]] = {
    # initial screen, earnings preview, event note (event summaries) ...
    "mission_deliverable": ("mission_deliverable_versions", "deliverable_ref", "subject_ref"),
    "company_dossier": ("company_dossier_versions", "dossier_ref", "company_ref"),
    "debate_map": ("debate_map_versions", "map_ref", "subject_ref"),
    "company_scenario": ("company_scenario_versions", "scenario_ref", "company_ref"),
    "deep_insight_gate": ("deep_insight_gate_versions", "gate_ref", "company_ref"),
    "industry_framework": ("industry_framework_versions", "framework_ref", "industry_ref"),
    "event_judgement": ("event_judgements", "event_ref", "company_ref", "created_at"),
    "zero_base_review": ("zero_base_review_versions", "review_ref", "company_ref"),
    "thesis": ("thesis_versions", "thesis_id", None, "version_number", "content_json"),
}
#: A product kind whose versions are point-in-time records (an event's
#: judgement) is only rewritten while it is recent; older ones are history,
#: graded by the outcome checks rather than redrafted.
PRODUCT_ACTIVE_DAYS: Mapping[str, int] = {"event_judgement": 14}
PRODUCT_LABELS = {
    "initial_screen": "初筛", "company_dossier": "公司档案", "debate_map": "分歧图",
    "company_scenario": "场景", "earnings_preview": "业绩前瞻", "event_note": "事件汇总",
    "deep_insight_gate": "深度研究门", "industry_framework": "行业框架",
    "event_judgement": "事件研判", "zero_base_review": "零基审查", "thesis": "论点",
    "investment_memo": "投资备忘录", "weekly_brief": "周报", "earnings_calibration": "业绩校准",
    "industry_model": "行业模型", "company_model": "公司模型", "forecast_lines": "预测行",
}
#: What has to arrive before a flagged product is rewritten: the subject's
#: own disclosures first (evidence-index kinds, ``evidence_corpus.KINDS``),
#: for the latest reported period.  The material acquisition service reads
#: this; a lane rewrites only once one of them has arrived.
REPLACEMENT_MATERIAL: tuple[dict[str, Any], ...] = (
    {"kind": "sec_exhibit", "period": "latest_reported_quarter",
     "source_priority": ["8-K Exhibit 99.1 earnings release", "company investor-relations release"]},
    {"kind": "transcript", "period": "latest_reported_quarter",
     "source_priority": ["company earnings call transcript"]},
    {"kind": "sec_10q", "period": "latest_reported_quarter",
     "source_priority": ["10-Q", "10-K"]},
    {"kind": "web_page", "period": "latest_reported_quarter",
     "source_priority": ["company investor-relations official metrics page"]},
)


class ProductRewriteFlagError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _table_exists(connection: Any, name: str) -> bool:
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _walk(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))
    elif isinstance(value, str):
        for match in _CLAIM_RE.finditer(value):
            yield path, match.group(0)


_LABEL_KEYS = ("title", "aspect", "question", "name", "key", "section_key", "label")


def section_label(kind: str, record: Mapping[str, Any], path: Sequence[Any]) -> str:
    """The section of a product a cited Claim sits in, in the product's own words."""

    if not path:
        return "(全文)"
    head = path[0]
    if kind == "company_scenario":
        return f"{record.get('name') or record.get('key') or '场景'} / {head}"
    node: Any = record.get(head) if isinstance(record, Mapping) else None
    if isinstance(node, list) and len(path) > 1 and isinstance(path[1], int) \
            and 0 <= path[1] < len(node) and not isinstance(node[path[1]], Mapping):
        return str(head)  # a list of refs is one section, not one per ref
    if len(path) > 1 and isinstance(node, list) and isinstance(path[1], int) \
            and 0 <= path[1] < len(node) and isinstance(node[path[1]], Mapping):
        item = node[path[1]]
        name = next((str(item[key]) for key in _LABEL_KEYS if isinstance(item.get(key), str)
                     and item.get(key)), None)
        if name is None:
            return str(head)  # unnamed entries (a list of evidence refs) are one section
        if kind == "debate_map":
            side = path[2] if len(path) > 2 and isinstance(path[2], str) else None
            return name[:120] + ("" if side is None else f" / {side}")
        if kind == "mission_deliverable" or head == "sections":
            return name[:120]
        return f"{head} / {name[:100]}"
    return str(head)


def _spec(entry: Sequence[Any]) -> tuple[str, str, str | None, str, str]:
    table, identity, subject = entry[0], entry[1], entry[2]
    order = entry[3] if len(entry) > 3 else "version_number"
    record = entry[4] if len(entry) > 4 else "record_json"
    return table, identity, subject, order, record


def current_versions(connection: Any) -> list[dict[str, Any]]:
    """Every product identity's current version: kind, refs, subject and record."""

    out: list[dict[str, Any]] = []
    for kind, entry in PRODUCT_TABLES.items():
        table, identity, subject, order, record_column = _spec(entry)
        if not _table_exists(connection, table):
            continue
        version_column = "version_id" if _has_column(connection, table, "version_id") else identity
        if order == "created_at" and _has_column(connection, table, "judgement_id"):
            version_column = "judgement_id"
        subject_sql = f"t.{subject}" if subject else f"t.{identity}"
        window = ""
        active = PRODUCT_ACTIVE_DAYS.get(kind)
        if active is not None and _has_column(connection, table, "created_at"):
            since = (datetime.now(timezone.utc) - timedelta(days=active)).isoformat()
            window = f" WHERE t.created_at >= '{since}'"
        rows = connection.execute(
            f"SELECT t.{version_column} AS version_id, t.{identity} AS product_ref, "
            f"{subject_sql} AS subject_ref, t.{order} AS ordinal, "
            f"t.{record_column} AS record_json FROM {table} t "
            f"JOIN (SELECT {identity} AS ident, MAX({order}) AS top FROM {table} "
            f"GROUP BY {identity}) m ON m.ident=t.{identity} AND m.top=t.{order}" + window
        ).fetchall()
        for row in rows:
            try:
                record = json.loads(row["record_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(record, Mapping):
                continue
            product_kind = (str(record.get("kind")) if kind == "mission_deliverable"
                            and record.get("kind") else kind)
            out.append({"table_kind": kind, "product_kind": product_kind,
                        "product_ref": row["product_ref"], "subject_ref": row["subject_ref"],
                        "product_version_ref": row["version_id"],
                        "version_number": row["ordinal"], "record": record})
    return out


def _has_column(connection: Any, table: str, column: str) -> bool:
    try:
        return any(row[1] == column for row in connection.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def retired_for_rewrite(connection: Any,
                        reasons: frozenset[str] | None = None) -> dict[str, dict[str, Any]]:
    """claim_version_ref -> its standing retirement (every reason, unless narrowed)."""

    from .claim_retirement import retired_claim_version_refs

    if not (_table_exists(connection, "claim_retirement_challenges")
            and _table_exists(connection, "claim_retirement_decisions")):
        return {}
    standing = retired_claim_version_refs(connection)
    rows = connection.execute(
        "SELECT c.claim_version_ref AS ref, c.reason_code AS reason, d.decision_id AS decision_ref, "
        "c.record_json AS challenge_json FROM claim_retirement_challenges c "
        "JOIN claim_retirement_decisions d ON d.challenge_ref=c.challenge_id "
        "WHERE d.decision='retired'").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["ref"] not in standing or (reasons is not None and row["reason"] not in reasons):
            continue
        try:
            evidence = json.loads(row["challenge_json"]).get("evidence") or {}
        except (TypeError, ValueError):
            evidence = {}
        out[row["ref"]] = {"reason_code": row["reason"], "decision_ref": row["decision_ref"],
                           "table_issuer": evidence.get("table_issuer")}
    return out


#: Bookkeeping keys that list a product's citations without being a section
#: of its text (a dossier's ``unit_provenance`` lists every Claim it read).
LEDGER_KEYS = frozenset({"unit_provenance", "input_fingerprints", "change_evidence_refs",
                         "drafted_at", "bindings", "drafted_by", "verified_by",
                         # the union of every ref a version cites (gate, framework):
                         # its sections are read on their own
                         "evidence_refs", "upstream_bindings"})


def affected_sections(kind: str, record: Mapping[str, Any],
                      retired: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each section of this product version that needs a rewrite, and why.

    ``immediate``: it cites a Claim retired for an ``IMMEDIATE_REASONS``
    reason.  ``threshold``: fewer than ``VALID_CITATION_SHARE`` of the Claims
    it cites still stand.  A section citing a few retired Claims among many
    standing ones is left as it is (the retired-citation display covers it).
    """

    cited: dict[str, set[str]] = {}
    for path, ref in _walk(record):
        if path and path[0] in LEDGER_KEYS:
            continue
        cited.setdefault(section_label(kind, record, path), set()).add(ref)
    out = []
    for name, refs in sorted(cited.items()):
        gone = sorted(ref for ref in refs if ref in retired)
        if not gone:
            continue
        valid_share = (len(refs) - len(gone)) / len(refs)
        reasons = sorted({retired[ref]["reason_code"] for ref in gone})
        if any(reason in IMMEDIATE_REASONS for reason in reasons):
            severity = "immediate"
        elif valid_share == 0:
            severity = "high"
        elif valid_share < VALID_CITATION_SHARE:
            severity = "threshold"
        else:
            continue
        out.append({"section": name, "claim_version_refs": gone, "cited": len(refs),
                    "valid_share": round(valid_share, 4), "severity": severity,
                    "retirement_reasons": reasons})
    return out


def products_to_flag(connection: Any,
                     reasons: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """Every current product version with a section that needs a rewrite."""

    retired = retired_for_rewrite(connection, reasons)
    if not retired:
        return []
    found = []
    for product in current_versions(connection):
        sections = affected_sections(product["table_kind"], product["record"], retired)
        if not sections:
            continue
        refs = sorted({ref for item in sections for ref in item["claim_version_refs"]})
        found.append({
            "product_kind": product["product_kind"], "product_ref": product["product_ref"],
            "product_version_ref": product["product_version_ref"],
            "subject_ref": product["subject_ref"], "sections": sections,
            "claim_version_refs": refs,
            "retirement_reasons": sorted({retired[ref]["reason_code"] for ref in refs}),
            "decision_refs": sorted({retired[ref]["decision_ref"] for ref in refs}),
            "severity": next(level for level in SEVERITIES
                             if any(item["severity"] == level for item in sections)),
        })
    return found


def citation_display(kind: str, record: Mapping[str, Any], retired: Mapping[str, Any] | set[str]
                     ) -> list[dict[str, Any]]:
    """Every section of a product that cites a retired Claim, as it is shown.

    Every product kind, every retirement reason: until the product is
    rewritten, a paragraph resting on a retired Claim is rendered as
    「历史引用」 (``display_label``) -- marked, never silently current and
    never deleted.  ``needs_rewrite`` says which of them must be rewritten.
    """

    cited: dict[str, set[str]] = {}
    for path, ref in _walk(record):
        if path and path[0] in LEDGER_KEYS:
            continue
        cited.setdefault(section_label(kind, record, path), set()).add(ref)
    out = []
    for name, refs in sorted(cited.items()):
        gone = sorted(ref for ref in refs if ref in retired)
        if not gone:
            continue
        reasons = sorted({str((retired.get(ref) or {}).get("reason_code"))
                          for ref in gone}) if isinstance(retired, Mapping) else []
        out.append({"section": name, "retired_claim_refs": gone, "cited": len(refs),
                    "valid_share": round((len(refs) - len(gone)) / len(refs), 4),
                    "retirement_reasons": [reason for reason in reasons if reason != "None"],
                    "display_label": HISTORICAL_CITATION_LABEL, "display_note": HISTORICAL_CITATION_NOTE})
    return out


def needs_rewrite(connection: Any, *, subject_ref: str | None = None,
                  product_kind: str | None = None,
                  arrivals: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Open flags: the version each names is still its product's current version.

    What a product lane reads when it plans its next batched redraft; a flag
    on a version the lane has since replaced is answered and not returned.
    ``arrivals`` (``evidence_index.material_arrivals`` rows) decide
    ``rewrite_ready``: a flagged product is rewritten only after replacement
    material of a required kind has arrived for its subject since the flag;
    until then it is ``awaiting_material`` -- marked, not rewritten.
    """

    if not _table_exists(connection, "product_rewrite_flags"):
        return []
    current = {item["product_version_ref"] for item in current_versions(connection)}
    out = []
    for row in connection.execute(
            "SELECT record_json FROM product_rewrite_flags ORDER BY created_at, flag_id").fetchall():
        record = json.loads(row["record_json"])
        if record["product_version_ref"] not in current:
            continue
        if subject_ref is not None and record.get("subject_ref") != subject_ref:
            continue
        if product_kind is not None and record.get("product_kind") != product_kind:
            continue
        arrived = [item for item in arrivals or ()
                   if item.get("company_ref") == record.get("subject_ref")
                   and str(item.get("kind")) in {str(need["kind"]) for need in
                                                  record.get("requires_material") or ()}
                   and str(item.get("indexed_at") or item.get("acquired_at") or "")
                   >= str(record.get("created_at") or "")]
        out.append({**record, "status": "needs_rewrite",
                    "rewrite_ready": bool(arrived),
                    "rewrite_gate": "material_arrived" if arrived else "awaiting_material",
                    "arrived_material": [item.get("document_ref") for item in arrived][:20]})
    return out


class ProductRewriteFlagAuthority:
    """Append-only needs_rewrite flags over untouched product versions."""

    _authorized = authorized_flag()

    def __init__(self, store: DaltonStore, *, clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self.connection = store.connection
        self.clock = clock or _now
        self._authorization_flag = authorization_flag(
            self.connection, "dalton_product_rewrite_flag_authorized")
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self._authorized:
            raise RuntimeError("ProductRewriteFlagAuthority operation cannot be nested")
        self._authorized = True
        try:
            with self.store._transaction() as cur:
                yield cur
        finally:
            self._authorized = False

    def flag(self, item: Mapping[str, Any], *, actor_ref: str) -> dict[str, Any]:
        if not re.fullmatch(r"(automation|human):[A-Za-z0-9._:-]{1,128}", str(actor_ref)):
            raise ProductRewriteFlagError("actor_ref must be automation:… or human:…")
        record = {
            "schema_version": SCHEMA_VERSION, "rule_ref": RULE_REF,
            "status": "needs_rewrite",
            "product_kind": str(item["product_kind"]), "product_ref": str(item["product_ref"]),
            "product_version_ref": str(item["product_version_ref"]),
            "subject_ref": item.get("subject_ref"),
            "sections": [dict(section) for section in item["sections"]],
            "claim_version_refs": list(item["claim_version_refs"]),
            "retirement_reasons": list(item["retirement_reasons"]),
            "decision_refs": list(item["decision_refs"]),
            "severity": item.get("severity", "immediate"),
            "requires_material": [dict(need) for need in REPLACEMENT_MATERIAL],
            "rewrite_policy": "batched",
            "note": ("这一版引用了已退休的结论（发行人归属错误立即触发；其他任何退休原因在有效引用占比低于 "
                     f"{int(VALID_CITATION_SHARE * 100)}% 时触发，全部引用均已退休为高优先级）；受影响的章节需在替代材料到位后的下一次"
                     "批量重写中重写（不仅删去引用，还要清理由这些结论构成的推理链）。"),
            "actor_ref": str(actor_ref), "created_at": self.clock(),
        }
        record["id"] = "product-rewrite-flag:" + content_hash({
            "version": record["product_version_ref"], "claims": record["claim_version_refs"],
            "rule": RULE_REF})[:32]
        record["content_hash"] = content_hash({k: v for k, v in record.items()
                                               if k != "content_hash"})
        with self._transaction() as cur:
            existing = cur.execute("SELECT record_json FROM product_rewrite_flags WHERE flag_id=?",
                                   (record["id"],)).fetchone()
            if existing is not None:
                return {**json.loads(existing["record_json"]), "write_status": "duplicate"}
            cur.execute(
                "INSERT INTO product_rewrite_flags(flag_id,product_kind,product_ref,"
                "product_version_ref,subject_ref,rule_ref,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record["id"], record["product_kind"], record["product_ref"],
                 record["product_version_ref"], record["subject_ref"], RULE_REF,
                 json.dumps(record, ensure_ascii=False, sort_keys=True), record["content_hash"],
                 record["actor_ref"], record["created_at"]))
        return {**record, "write_status": "fresh"}

    def scan_probe(self) -> str:
        """Moves whenever a retirement or a product version is appended."""

        from .claim_retirement import retirement_state_probe

        parts = [retirement_state_probe(self.connection)]
        for _kind, entry in sorted(PRODUCT_TABLES.items()):
            table = _spec(entry)[0]
            if _table_exists(self.connection, table):
                row = self.connection.execute(f"SELECT COUNT(*), MAX(rowid) FROM {table}").fetchone()
                parts.append(f"{table}:{row[0]}:{row[1]}")
        return content_hash({"rule": RULE_REF, "parts": parts})

    def flag_all(self, *, actor_ref: str) -> dict[str, Any]:
        """Flag every current product version with a section that needs a rewrite.

        Reading every product is not free (legacy holds thousands of event
        judgements), so a scan is skipped while nothing it reads has moved.
        """

        probe = self.scan_probe()
        if self.connection.execute("SELECT 1 FROM product_rewrite_scans WHERE probe=?",
                                   (probe,)).fetchone() is not None:
            return {"flagged": [], "already_flagged": 0, "skipped": "unchanged"}
        flagged, unchanged = [], 0
        for item in products_to_flag(self.connection):
            result = self.flag(item, actor_ref=actor_ref)
            if result["write_status"] == "fresh":
                flagged.append({key: result[key] for key in (
                    "id", "product_kind", "product_ref", "product_version_ref", "subject_ref")}
                    | {"sections": [s["section"] for s in result["sections"]],
                       "claims": len(result["claim_version_refs"])})
            else:
                unchanged += 1
        with self._transaction() as cur:
            cur.execute("INSERT OR IGNORE INTO product_rewrite_scans(probe,scanned_at) VALUES(?,?)",
                        (probe, self.clock()))
        return {"flagged": flagged, "already_flagged": unchanged}


__all__ = [
    "HISTORICAL_CITATION_LABEL",
    "HISTORICAL_CITATION_NOTE",
    "SEVERITIES",
    "citation_display",
    "IMMEDIATE_REASONS",
    "PRODUCT_LABELS",
    "ProductRewriteFlagAuthority",
    "REPLACEMENT_MATERIAL",
    "REWRITE_REASONS",
    "VALID_CITATION_SHARE",
    "RULE_REF",
    "affected_sections",
    "current_versions",
    "needs_rewrite",
    "products_to_flag",
    "retired_for_rewrite",
    "section_label",
]
