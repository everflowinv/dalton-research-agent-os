"""W4: was the decision not to move the right one?  Derived, frozen, no model.

Chem's coverage agent produced 89 ``NO_CHANGE`` verdicts out of 92 and had no
way to tell which of them were judgement and which were inertia.  We inherited
the good half of that -- our judgement lane records ``no_change`` *with a
reason* -- and the missing half with it: nothing in this system has ever gone
back and asked whether the market subsequently disagreed.

This module is that ledger.  Two checks, both entirely derived:

**A ``no_change`` that the price then argued with.**  Reusing, deliberately
and by import, the arithmetic the ``price_divergence`` detector already uses:
the equal-weight basket of the *other* covered companies, the same
``divergence_vs_basket_percent`` threshold, the same ``window_trading_days``
window, and the same reading of "against" -- a long thesis wants the excess
positive.  A different threshold here would mean the system held two opinions
about what counts as a divergence.  Past it, the check is
``should_have_moved`` -- a **candidate**, in the ADR-0007 sense: a sentence
automation may form and only a person may act on.  Nothing consumes it.

**A ``revise`` that a later actual confirmed.**  The decision word carries a
direction (``THESIS_STRENGTHENED`` up, ``THESIS_WEAKENED`` and
``THESIS_BROKEN`` down).  The first ``ForecastReconciliation`` written for
that company after the judgement carries another: an actual above the forecast
is up, below is down.  The two agreeing is ``moved_right``.

Three rules keep this honest rather than flattering:

1. **Nothing is scored that cannot be scored yet.**  A window with fewer than
   N settled sessions in it is ``pending``, not ``held``.  A company with no
   price series, a basket too thin to be a basket, a decision word with no
   direction: ``unavailable`` with the reason, never a zero.
2. **The formula is versioned and every input is kept.**  ``FORMULA_REF``
   changes when the arithmetic changes, so an old reading stays legible as an
   old reading instead of being silently restated.
3. **There is no model call and no wall clock.**  ``now`` is injected; the
   only dates that matter are settled trading days that are already in the
   Ledger.  Two runs over the same rows produce the same ``inputs_hash`` and
   the second one writes nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from .market_event import DEFAULT_THESIS_STANCE, THESIS_STANCES, cumulative_return
from .market_price import bar_is_provisional
from .store import canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("judgement_outcome_schema.sql")

#: Bumped when the arithmetic below changes what it means.  Part of every
#: check's identity, so a re-reading under a new formula is a new version of
#: the check rather than a quiet correction of the old one.
FORMULA_REF = "judgement-outcome-formula:w4:v1"

#: The two shapes of a past decision this module can grade, and the actions
#: that fall into each.  ``note`` and ``research`` are neither: they change no
#: view, so "was it right" has no content.
NO_CHANGE_ACTIONS: frozenset[str] = frozenset({"no_change"})
REVISE_ACTIONS: frozenset[str] = frozenset({
    "revise_forecast", "revise_thesis", "revise_dossier",
})

#: What each decision word implies about the company's direction.  Named
#: rather than inferred: ``NEW_THESIS`` genuinely has no direction, and
#: guessing one for it would manufacture a grade out of nothing.
DIRECTION_BY_DECISION: Mapping[str, str] = {
    "THESIS_STRENGTHENED": "up",
    "THESIS_WEAKENED": "down",
    "THESIS_BROKEN": "down",
}

#: Closed.  ``should_have_moved`` and ``moved_right`` are the two the owner
#: asked for; the rest exist so that the counts add up to the judgements and
#: an absence never reads as a pass.
NO_CHANGE_OUTCOMES: tuple[str, ...] = (
    "should_have_moved", "held", "pending", "unavailable",
)
REVISE_OUTCOMES: tuple[str, ...] = (
    "moved_right", "moved_wrong", "not_confirmed", "pending", "unavailable",
)
OUTCOMES: tuple[str, ...] = tuple(
    dict.fromkeys(NO_CHANGE_OUTCOMES + REVISE_OUTCOMES)
)

MAX_REASON_CHARS = 600


class JudgementOutcomeError(RuntimeError):
    """Base error for the outcome ledger."""


class JudgementOutcomeValidationError(JudgementOutcomeError, ValueError):
    """A derived row does not satisfy the closed contract."""


class JudgementOutcomeConflict(JudgementOutcomeError):
    """An append-only record was reused with different semantics."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JudgementOutcomeValidationError(f"{name} must be non-empty text")
    text = value.strip()
    if len(text) > maximum:
        raise JudgementOutcomeValidationError(f"{name} is longer than {maximum} characters")
    return text


def _day(value: Any) -> str | None:
    """The calendar day of a Core timestamp, or nothing if it cannot be read."""

    if not isinstance(value, str) or len(value) < 10:
        return None
    day = value[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def check_ref_for(judgement_ref: str) -> str:
    return "judgement-outcome:" + content_hash({"judgement": judgement_ref})[:32]


def check_kind_for(action: Any) -> str | None:
    """Which of the two checks a judgement's action earns, or neither."""

    if action in NO_CHANGE_ACTIONS:
        return "no_change"
    if action in REVISE_ACTIONS:
        return "revise"
    return None


# ---------------------------------------------------------------------------
# the sessions everyone shares
# ---------------------------------------------------------------------------


def settled_dates(series_by_company: Mapping[str, Mapping[str, Any] | None]) -> list[str]:
    """Every trading day every covered company has a settled bar for, oldest first.

    The intersection rather than the union, for the reason
    ``latest_settled_date`` gives: a day one company has and the others do not
    would make the basket one name wide, and a one-name basket is not a
    comparison.  A provisional bar is not a settled day.
    """

    days: list[set[str]] = []
    for series in series_by_company.values():
        if not series or not series.get("bars"):
            continue
        days.append({
            str(bar["date"]) for bar in series["bars"] if not bar_is_provisional(bar)
        })
    if not days:
        return []
    shared = set.intersection(*days) if len(days) > 1 else days[0]
    return sorted(shared)


def window_for(dates: Sequence[str], *, anchor: str, sessions: int) -> tuple[str, str] | None:
    """The last close at or before the decision, and the close N sessions later.

    ``None`` when the window has not finished yet, which is the case this
    returns most often and the one a naive implementation gets wrong by
    treating "not yet" as "no divergence".
    """

    if sessions < 1:
        raise JudgementOutcomeValidationError("a window is at least one session")
    before = [day for day in dates if day <= anchor]
    if not before:
        return None
    from_date = before[-1]
    after = [day for day in dates if day > from_date]
    if len(after) < sessions:
        return None
    return from_date, after[sessions - 1]


# ---------------------------------------------------------------------------
# the two checks
# ---------------------------------------------------------------------------


def _unavailable(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"check_kind": kind, "outcome": "unavailable",
            "reason": _text(reason, "reason", maximum=MAX_REASON_CHARS), **extra}


def no_change_check(
    *,
    judgement: Mapping[str, Any],
    series_by_company: Mapping[str, Mapping[str, Any] | None],
    dates: Sequence[str],
    thresholds: Mapping[str, Any],
    stance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Did the price argue with this decision not to move?

    The whole of the arithmetic is imported from the divergence detector's
    module, so "beyond the threshold" means here exactly what it means when
    the detector raises a ``price_divergence`` event.
    """

    company_ref = str(judgement["company_ref"])
    anchor = _day(judgement.get("created_at"))
    if anchor is None:
        return _unavailable("no_change", "这条判断没有可读的时间戳，无法定锚")
    threshold = _decimal(thresholds.get("divergence_vs_basket_percent"))
    if threshold is None:
        return _unavailable("no_change", "tracking policy 没有给出背离阈值")
    sessions = int(thresholds.get("window_trading_days") or 10)
    min_members = int(thresholds.get("min_basket_members") or 0)
    if stance is None:
        return _unavailable(
            "no_change", "这家公司没有可解析的 thesis 方向；背离的方向无从判断",
            anchor=anchor,
        )
    direction = str(stance.get("stance") or DEFAULT_THESIS_STANCE)
    if direction not in THESIS_STANCES:
        return _unavailable("no_change", f"thesis 方向 {direction!r} 不在词表里", anchor=anchor)
    if series_by_company.get(company_ref) is None:
        return _unavailable("no_change", "这家公司还没有股价序列", anchor=anchor)
    window = window_for(dates, anchor=anchor, sessions=sessions)
    if window is None:
        return {
            "check_kind": "no_change", "outcome": "pending", "anchor": anchor,
            "sessions": sessions, "settled_after_anchor": len(
                [day for day in dates if day > anchor]
            ),
            "reason": f"判断之后还没有 {sessions} 个结算交易日，现在下结论是在猜",
        }
    from_date, as_of = window
    cumulative: dict[str, Decimal] = {}
    for ref, series in series_by_company.items():
        value = cumulative_return(series, from_date=from_date, as_of=as_of)
        if value is not None:
            cumulative[ref] = value
    own = cumulative.get(company_ref)
    if own is None:
        return _unavailable(
            "no_change", "这家公司在这个窗口内算不出累计收益", anchor=anchor,
            from_date=from_date, as_of=as_of,
        )
    members = [value for ref, value in cumulative.items() if ref != company_ref]
    if len(members) < max(1, min_members):
        return _unavailable(
            "no_change",
            f"行业篮子只有 {len(members)} 个成员，少于 {max(1, min_members)}；"
            "一个人的篮子不是比较",
            anchor=anchor, from_date=from_date, as_of=as_of, basket_members=len(members),
        )
    basket = sum(members, Decimal(0)) / Decimal(len(members))
    excess = own - basket
    against = -excess if direction == "long" else excess
    return {
        "check_kind": "no_change",
        "outcome": "should_have_moved" if against >= threshold else "held",
        "anchor": anchor,
        "from_date": from_date,
        "as_of": as_of,
        "sessions": sessions,
        "cumulative_return_percent": str(own),
        "basket_return_percent": str(basket),
        "excess_vs_basket_percent": str(excess),
        "divergence_percent": str(against),
        "threshold_percent": str(threshold),
        "basket_members": len(members),
        "thesis_ref": stance.get("thesis_ref"),
        "thesis_stance": direction,
        "price_version_ref": (series_by_company.get(company_ref) or {}).get("version_ref"),
        "reason": (
            "价格在窗口内相对行业篮子朝着与我们判断相反的方向走过了阈值；这是一条"
            "「当时该动没动」候选，不是结论"
            if against >= threshold else
            "价格在窗口内没有相对行业篮子背离到阈值以上"
        ),
    }


def revise_check(
    *,
    judgement: Mapping[str, Any],
    reconciliations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Did a later actual confirm the direction this revision took?

    ``reconciliations`` are this company's rows, oldest first; the first one
    written after the judgement is the one that settles it.  The first rather
    than the best: picking the reconciliation that agrees is how a ledger
    becomes a scoreboard.
    """

    decision = str(judgement.get("decision") or "")
    implied = DIRECTION_BY_DECISION.get(decision)
    if implied is None:
        return _unavailable(
            "revise", f"决定词 {decision!r} 不含方向，无从证实或证伪", decision=decision,
        )
    created_at = str(judgement.get("created_at") or "")
    later = [
        row for row in reconciliations
        if str(row.get("created_at") or "") > created_at
    ]
    if not later:
        return {"check_kind": "revise", "outcome": "pending", "decision": decision,
                "implied_direction": implied,
                "reason": "这次修订之后还没有一条预测与实际的对账，方向无从证实"}
    row = later[0]
    forecast = _decimal(row.get("forecast_value"))
    actual = _decimal(row.get("actual_value"))
    if forecast is None or actual is None:
        return _unavailable(
            "revise", "对账记录里的预测值或实际值读不出来", decision=decision,
            reconciliation_ref=row.get("id"),
        )
    if actual > forecast:
        actual_direction = "up"
    elif actual < forecast:
        actual_direction = "down"
    else:
        actual_direction = "flat"
    if actual_direction == "flat":
        outcome = "not_confirmed"
        reason = "实际值与预测值相同，方向既没被证实也没被证伪"
    elif actual_direction == implied:
        outcome = "moved_right"
        reason = "后来的实际值证实了这次修订的方向"
    else:
        outcome = "moved_wrong"
        reason = "后来的实际值与这次修订的方向相反"
    return {
        "check_kind": "revise",
        "outcome": outcome,
        "decision": decision,
        "implied_direction": implied,
        "actual_direction": actual_direction,
        "reconciliation_ref": row.get("id"),
        "reconciliation_at": row.get("created_at"),
        "metric_ref": row.get("metric_ref"),
        "period_end": row.get("period_end"),
        "forecast_value": str(forecast),
        "actual_value": str(actual),
        "band": row.get("band"),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# reading the two ledgers this needs
# ---------------------------------------------------------------------------


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def gradable_judgements(
    connection: sqlite3.Connection, *, company_refs: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Every judgement whose action changes a view, oldest first."""

    if not _table_exists(connection, "event_judgements"):
        return []
    query = (
        "SELECT judgement_id, event_ref, company_ref, decision, action, created_at "
        "FROM event_judgements"
    )
    params: list[Any] = []
    if company_refs:
        query += " WHERE company_ref IN (" + ",".join("?" * len(company_refs)) + ")"
        params.extend(company_refs)
    query += " ORDER BY created_at, judgement_id"
    rows = connection.execute(query, params).fetchall()
    graded = []
    for row in rows:
        kind = check_kind_for(row["action"])
        if kind is None:
            continue
        graded.append({
            "id": row["judgement_id"], "event_ref": row["event_ref"],
            "company_ref": row["company_ref"], "decision": row["decision"],
            "action": row["action"], "created_at": row["created_at"],
            "check_kind": kind,
        })
    return graded


def reconciliations_by_company(
    connection: sqlite3.Connection, *, company_refs: Sequence[str] = ()
) -> dict[str, list[dict[str, Any]]]:
    """Forecast-versus-actual rows per company, oldest first.

    Reads ``record_json`` rather than the columns because the two numbers the
    check needs -- the forecast and the actual -- live there and not in the
    index columns.
    """

    if not _table_exists(connection, "forecast_reconciliations"):
        return {}
    query = (
        "SELECT reconciliation_id, subject_ref, metric_ref, period_end, band, "
        "record_json, created_at FROM forecast_reconciliations"
    )
    params: list[Any] = []
    if company_refs:
        query += " WHERE subject_ref IN (" + ",".join("?" * len(company_refs)) + ")"
        params.extend(company_refs)
    query += " ORDER BY created_at, reconciliation_id"
    by_company: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(query, params).fetchall():
        try:
            record = json.loads(row["record_json"])
        except ValueError:  # pragma: no cover - a row nothing could have written
            record = {}
        by_company.setdefault(str(row["subject_ref"]), []).append({
            "id": row["reconciliation_id"],
            "metric_ref": row["metric_ref"],
            "period_end": row["period_end"],
            "band": row["band"],
            "created_at": row["created_at"],
            "forecast_value": record.get("forecast_value"),
            "actual_value": record.get("actual_value"),
        })
    return by_company


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


def build_outcome_checks(
    connection: sqlite3.Connection,
    *,
    company_refs: Sequence[str],
    series_by_company: Mapping[str, Mapping[str, Any] | None],
    thresholds: Mapping[str, Any],
    stances: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """One derived row per gradable judgement, in judgement order.

    Read-only and side-effect free: the lane tick calls it to decide whether
    anything has moved, the child calls it to write what it found, and the two
    are the same computation by construction rather than by agreement.
    """

    dates = settled_dates(series_by_company)
    reconciliations = reconciliations_by_company(connection, company_refs=company_refs)
    checks: list[dict[str, Any]] = []
    for judgement in gradable_judgements(connection, company_refs=company_refs):
        if judgement["check_kind"] == "no_change":
            body = no_change_check(
                judgement=judgement, series_by_company=series_by_company,
                dates=dates, thresholds=thresholds,
                stance=stances.get(judgement["company_ref"]),
            )
        else:
            body = revise_check(
                judgement=judgement,
                reconciliations=reconciliations.get(judgement["company_ref"], []),
            )
        inputs = {key: value for key, value in body.items() if key != "reason"}
        checks.append({
            "schema_version": SCHEMA_VERSION,
            "formula_ref": FORMULA_REF,
            "check_ref": check_ref_for(judgement["id"]),
            "judgement_ref": judgement["id"],
            "event_ref": judgement["event_ref"],
            "company_ref": judgement["company_ref"],
            "judgement_decision": judgement["decision"],
            "judgement_action": judgement["action"],
            "judged_at": judgement["created_at"],
            "inputs_hash": content_hash({"formula": FORMULA_REF, "inputs": inputs}),
            **body,
        })
    return checks


def checks_digest(checks: Sequence[Mapping[str, Any]]) -> str:
    """What makes two passes over the ledger the same pass."""

    return content_hash({
        "formula": FORMULA_REF,
        "checks": [
            {"check_ref": row["check_ref"], "inputs_hash": row["inputs_hash"]}
            for row in checks
        ],
    })


def count_outcomes(checks: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {outcome: 0 for outcome in OUTCOMES}
    for row in checks:
        outcome = str(row.get("outcome") or "")
        if outcome in counts:
            counts[outcome] += 1
    return counts


# ---------------------------------------------------------------------------
# the authority
# ---------------------------------------------------------------------------


def _decode(row: sqlite3.Row, name: str) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    if record.get("content_hash") != row["content_hash"]:
        raise JudgementOutcomeConflict(f"{name} did not read back as written")
    return record


class JudgementOutcomeAuthority:
    """Append-only derived checks, one version chain per judgement.

    The only writer, and it writes two tables of its own.  It opens no Claim,
    no thesis, no deliverable and no mission: a record about whether a past
    decision was right must not be able to make a new one.
    """

    def __init__(self, store: Any, *, clock: Callable[[], str] | None = None) -> None:
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("JudgementOutcomeAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.clock = clock or _now

    # -- reading -----------------------------------------------------------

    def latest(self, check_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM judgement_outcome_check_pointer p "
            "JOIN judgement_outcome_check_versions v ON v.version_id = p.version_id "
            "WHERE p.check_ref = ?", (_text(check_ref, "check_ref"),),
        ).fetchone()
        return None if row is None else _decode(row, "JudgementOutcomeCheck")

    def versions(self, check_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT record_json, content_hash FROM judgement_outcome_check_versions "
            "WHERE check_ref = ? ORDER BY version_number",
            (_text(check_ref, "check_ref"),),
        ).fetchall()
        return [_decode(row, "JudgementOutcomeCheck") for row in rows]

    def for_judgement(self, judgement_ref: str) -> dict[str, Any] | None:
        return self.latest(check_ref_for(_text(judgement_ref, "judgement_ref")))

    def current(self, company_ref: str | None = None) -> list[dict[str, Any]]:
        """The newest version of every check, oldest judgement first."""

        query = (
            "SELECT v.record_json AS record_json, v.content_hash AS content_hash "
            "FROM judgement_outcome_check_pointer p "
            "JOIN judgement_outcome_check_versions v ON v.version_id = p.version_id"
        )
        params: list[Any] = []
        if company_ref is not None:
            query += " WHERE p.company_ref = ?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY v.created_at, v.version_id"
        rows = self.connection.execute(query, params).fetchall()
        return [_decode(row, "JudgementOutcomeCheck") for row in rows]

    def counts(self, company_ref: str | None = None) -> dict[str, int]:
        return count_outcomes(self.current(company_ref))

    # -- writing -----------------------------------------------------------

    def record(self, body: Mapping[str, Any], *, actor_ref: str) -> dict[str, Any]:
        """Write one check, or return the identical one already written."""

        actor_ref = _text(actor_ref, "actor_ref", maximum=256)
        if not actor_ref.startswith("automation:"):
            raise JudgementOutcomeValidationError(
                "a derived check is written by an automation principal; a person "
                "deciding something is a different record"
            )
        check_ref = _text(body.get("check_ref"), "check_ref")
        judgement_ref = _text(body.get("judgement_ref"), "judgement_ref")
        if check_ref != check_ref_for(judgement_ref):
            raise JudgementOutcomeValidationError(
                "check_ref is derived from judgement_ref and did not match"
            )
        company_ref = _text(body.get("company_ref"), "company_ref")
        kind = body.get("check_kind")
        if kind not in ("no_change", "revise"):
            raise JudgementOutcomeValidationError(
                "check_kind is no_change or revise"
            )
        outcome = body.get("outcome")
        permitted = NO_CHANGE_OUTCOMES if kind == "no_change" else REVISE_OUTCOMES
        if outcome not in permitted:
            raise JudgementOutcomeValidationError(
                f"a {kind} check reports one of {list(permitted)}"
            )
        if body.get("formula_ref") != FORMULA_REF:
            raise JudgementOutcomeValidationError(
                f"this build computes {FORMULA_REF}; a row claiming another "
                "formula was not produced by it"
            )
        digest = _text(body.get("inputs_hash"), "inputs_hash", maximum=128)
        record = {
            **{key: value for key, value in body.items() if key != "content_hash"},
            "actor_ref": actor_ref,
            "created_at": self.clock(),
        }
        with self.store._transaction() as cur:
            seen = cur.execute(
                "SELECT record_json, content_hash FROM judgement_outcome_check_versions "
                "WHERE check_ref = ? AND inputs_hash = ?", (check_ref, digest),
            ).fetchone()
            if seen is not None:
                return {**_decode(seen, "JudgementOutcomeCheck"), "status": "duplicate"}
            pointer = cur.execute(
                "SELECT version_id, version_number FROM judgement_outcome_check_pointer "
                "WHERE check_ref = ?", (check_ref,),
            ).fetchone()
            version = 1 if pointer is None else int(pointer["version_number"]) + 1
            prior = None if pointer is None else pointer["version_id"]
            record["version"] = version
            record["prior_version_ref"] = prior
            record["id"] = (
                "judgement-outcome-version:"
                + content_hash({"ref": check_ref, "version": version})[:32]
            )
            record["content_hash"] = content_hash(
                {key: value for key, value in record.items() if key != "content_hash"}
            )
            cur.execute(
                "INSERT INTO judgement_outcome_check_versions(version_id,check_ref,"
                "version_number,prior_version_id,judgement_ref,company_ref,check_kind,"
                "outcome,formula_ref,inputs_hash,record_json,content_hash,actor_ref,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], check_ref, version, prior, judgement_ref, company_ref,
                 kind, outcome, FORMULA_REF, digest, canonical_json(record),
                 record["content_hash"], actor_ref, record["created_at"]),
            )
            if pointer is None:
                cur.execute(
                    "INSERT INTO judgement_outcome_check_pointer(check_ref,version_id,"
                    "version_number,judgement_ref,company_ref,check_kind,outcome,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (check_ref, record["id"], version, judgement_ref, company_ref, kind,
                     outcome, record["content_hash"], record["created_at"]),
                )
            else:
                cur.execute(
                    "UPDATE judgement_outcome_check_pointer SET version_id=?, "
                    "version_number=?, outcome=?, content_hash=?, updated_at=? "
                    "WHERE check_ref=?",
                    (record["id"], version, outcome, record["content_hash"],
                     record["created_at"], check_ref),
                )
        written = self.latest(check_ref)
        if written is None or written["id"] != record["id"]:
            raise JudgementOutcomeConflict("the outcome check did not read back")
        return {**written, "status": "fresh"}

    def record_all(
        self, checks: Sequence[Mapping[str, Any]], *, actor_ref: str
    ) -> dict[str, Any]:
        """Write a whole pass, reporting what was new and what was not."""

        fresh = 0
        duplicate = 0
        for body in checks:
            written = self.record(body, actor_ref=actor_ref)
            if written["status"] == "fresh":
                fresh += 1
            else:
                duplicate += 1
        return {"checked": len(checks), "fresh": fresh, "duplicate": duplicate,
                "counts": count_outcomes(checks)}


__all__ = [
    "DIRECTION_BY_DECISION",
    "FORMULA_REF",
    "NO_CHANGE_ACTIONS",
    "NO_CHANGE_OUTCOMES",
    "OUTCOMES",
    "REVISE_ACTIONS",
    "REVISE_OUTCOMES",
    "SCHEMA_VERSION",
    "JudgementOutcomeAuthority",
    "JudgementOutcomeConflict",
    "JudgementOutcomeError",
    "JudgementOutcomeValidationError",
    "build_outcome_checks",
    "check_kind_for",
    "check_ref_for",
    "checks_digest",
    "count_outcomes",
    "gradable_judgements",
    "no_change_check",
    "reconciliations_by_company",
    "revise_check",
    "settled_dates",
    "window_for",
]
