"""Whether a piece of research prose may be shown as final, from the ledger.

2026-10-07 (cockpit inspection BUG-01 / BUG-02).  The page used to decide
"is this final Chinese?" by looking each sentence up in its reviewed-UI-text
dictionary.  That is a proxy, and it failed both ways: the weekly reflection,
whose publication had been given up (``exhausted``), was hidden for ever behind
"正文正在检查文字表达" although no check was running; and the ACN/DXC dossiers,
parked by the publication worker, exported empty sections for days.

The rule is now one function, keyed on the publication state of the exact
artifact version -- the reviewed attachment in ``research-localization`` (the
localization ledger) and, when there is none, the worker's own state for that
product in ``research-publication-work/products``:

``ready``
    a reviewed attachment exists for these exact source bytes (or the
    environment does not require language review).  Show the final text.
``pending``
    no reviewed attachment yet, and a review can still be under way: the
    version is younger than the draft window and the worker has not given up.
    2026-10-09 (financial review 痛点5): a blank "being checked" read as a
    crash.  The state now carries ``show_draft: true`` and ``badge``
    「初稿 · 文字未经审校」: a page shows the drafted text with that badge,
    and switches to the reviewed text as soon as there is one (``ready``
    always wins).  ``message`` stays for readers that still hide the text.
``draft``
    no reviewed attachment, and either the worker gave up on this version
    (``exhausted``) or it has waited longer than the draft window
    (``draft_fallback_after_hours`` in ``research-language-policy.json``,
    default one hour).  Show the drafted text itself, watermarked
    「草稿，文字表达尚未校对」, and offer 「重试语言质检」 when the worker has a
    state the owner may reopen.

Read-only: nothing here creates a directory, writes a file or calls a model.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

READY = "ready"
PENDING = "pending"
DRAFT = "draft"

PENDING_TEXT = "正文正在检查文字表达，完成后会显示。"
from .terminology import label as _term

DRAFT_LABEL = _term("report_state", "draft")
#: The badge a page puts on drafted text while its review may still run.
PENDING_BADGE = _term("report_state", "draft")
DEFAULT_DRAFT_AFTER_SECONDS = 60 * 60
POLICY_FILE = "research-language-policy.json"
WORKER_CONFIG_FILE = "research-publication-worker-config.json"
DEFAULT_WORK_DIRECTORY = "research-publication-work"

_MAX_POLICY_BYTES = 64 * 1024


def _read_small_json(path: Path) -> Any:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_POLICY_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def language_policy(state_dir: str | Path | None) -> dict[str, Any]:
    """``required`` and the draft window, from ``research-language-policy.json``.

    An unreadable policy file is read as "required" -- the conservative
    reading the localization store already makes -- with the default window.
    """

    if state_dir is None:
        return {"required": False, "draft_after_seconds": float(DEFAULT_DRAFT_AFTER_SECONDS)}
    path = Path(state_dir) / POLICY_FILE
    if not path.exists():
        return {"required": False, "draft_after_seconds": float(DEFAULT_DRAFT_AFTER_SECONDS)}
    policy = _read_small_json(path)
    if not isinstance(policy, Mapping):
        return {"required": True, "draft_after_seconds": float(DEFAULT_DRAFT_AFTER_SECONDS)}
    hours = policy.get("draft_fallback_after_hours")
    seconds = float(DEFAULT_DRAFT_AFTER_SECONDS)
    if (isinstance(hours, (int, float)) and not isinstance(hours, bool)
            and 0 <= float(hours) <= 24 * 30):
        seconds = float(hours) * 3600.0
    return {"required": policy.get("required") is True, "draft_after_seconds": seconds}


def publication_work_dir(state_dir: str | Path) -> Path:
    """The worker's ``work_dir``: its configuration's, else the default beside the Core."""

    root = Path(state_dir)
    config = _read_small_json(root / WORKER_CONFIG_FILE)
    if isinstance(config, Mapping) and isinstance(config.get("work_dir"), str) and config["work_dir"]:
        return Path(config["work_dir"]).expanduser()
    return root / DEFAULT_WORK_DIRECTORY


def worker_product_state(state_dir: str | Path, product: Mapping[str, Any]) -> dict[str, Any] | None:
    """The publication worker's state for this exact product, or ``None``.

    ``None`` when the worker has not reached this version, when its state is
    for other source bytes, or when anything about it is unreadable: the page
    then falls back to the time rule alone.
    """

    from .research_publication_worker import (
        _consumed_sequence, _hash, _identity, _read_state, failure_reasons,
        retry_chain, retry_class,
    )

    try:
        identity = _identity(product)
        identity_hash = _hash(identity)
        products = publication_work_dir(state_dir) / "products"
        state = _read_state(products / (identity_hash + ".json"))
    except (OSError, ValueError, TypeError):
        return None
    if state is None or state.get("product_hash") != _hash(product):
        return None
    requested = False
    if state.get("status") in {"pending", "exhausted"}:
        try:
            chain = retry_chain(products, identity_hash)
        except (OSError, ValueError):
            chain = []
        latest = chain[-1] if chain else None
        requested = bool(latest is not None and latest["product_hash"] == state["product_hash"]
                         and latest["sequence"] > _consumed_sequence(state))
    reasons = failure_reasons(state.get("result"))
    return {"status": state.get("status"), "identity_hash": identity_hash,
            "product_hash": state["product_hash"], "retry_class": retry_class(state.get("result")),
            "last_failure": reasons[0][:300] if reasons else None,
            "retry_requested": requested}


def _age_seconds(since: Any, now: datetime) -> float | None:
    if not isinstance(since, str) or not since:
        return None
    try:
        moment = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (now - moment).total_seconds()


def final_text_state(state_dir: str | Path | None, product: Mapping[str, Any] | None, *,
                     reviewed: bool, since: str | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """One artifact version's display state; see the module docstring."""

    policy = language_policy(state_dir)
    window = policy["draft_after_seconds"]
    if reviewed or not policy["required"]:
        return {"status": READY}
    now = now or datetime.now(timezone.utc)
    worker = (worker_product_state(state_dir, product)
              if state_dir is not None and isinstance(product, Mapping) else None)
    age = _age_seconds(since, now)
    # Given up: exhausted, or parked on a failure no automatic retry covers
    # (a changed number, a verifier rejection) with no owner retry asked --
    # the worker will not touch this version again, so nothing is "checking".
    gave_up = worker is not None and not worker["retry_requested"] and (
        worker["status"] == "exhausted"
        or (worker["status"] == "pending" and worker["retry_class"] is None
            and worker["last_failure"] is not None))
    overdue = age is not None and age >= window
    status = DRAFT if gave_up or overdue else PENDING
    state: dict[str, Any] = {"status": status, "since": since,
                             "draft_after_seconds": int(window)}
    if status == PENDING:
        state["message"] = PENDING_TEXT
        state["show_draft"] = True
        state["badge"] = PENDING_BADGE
    else:
        state["label"] = DRAFT_LABEL
        state["reason"] = ("文字校对没有通过，系统不会自动重试；可以点「重试语言质检」再试一次。" if gave_up
                           else f"文字校对已等待超过 {max(1, round(window / 3600))} 小时。")
    if worker is not None and worker["status"] in {"pending", "exhausted"}:
        state["retry"] = {"identity_hash": worker["identity_hash"],
                          "product_hash": worker["product_hash"],
                          "requested": worker["retry_requested"]}
    return state


def reviewed_product_state(connection: Any, product: Mapping[str, Any] | None, *,
                           since: str | None = None, entries: Mapping[str, Any] | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """``final_text_state`` for one projected product read through ``connection``."""

    from .research_localization_store import directory_for_connection, has_reviewed_attachment

    if product is None:
        # Nothing the worker would review: no prose to hold back.
        return {"status": READY}
    root = directory_for_connection(connection)
    reviewed = root is not None and has_reviewed_attachment(root, product, entries=entries)
    return final_text_state(None if root is None else root.parent, product,
                            reviewed=reviewed, since=since, now=now)


def surface_states(connection: Any, mission: Mapping[str, Any], wanted: Any, *,
                   now: datetime | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """States for ``(kind, version_ref, company_ref, since)`` surface items.

    Builds, per company, only the product kinds asked for -- the exact
    projection the publication worker reviews -- and answers each from the
    ledger.  An item whose version is no longer one the worker reviews (an
    older reflection, a decided gate) is simply absent: the page then keeps
    its previous behaviour for it.
    """

    from .final_surface_products import final_surface_products
    from .research_localization_store import directory_for_connection, reviewed_index_entries

    universe = [row.get("company_ref") for row in mission.get("universe") or []
                if isinstance(row, Mapping) and isinstance(row.get("company_ref"), str)]
    requests: dict[str, dict[tuple[str, str], str | None]] = {}
    for kind, version_ref, company_ref, since in wanted:
        if not isinstance(version_ref, str):
            continue
        # A card that does not name its company is looked for in each one.
        for company in ([company_ref] if isinstance(company_ref, str) else universe):
            requests.setdefault(company, {})[(kind, version_ref)] = since
    if not requests or not language_policy(_state_dir(connection))["required"]:
        return {}
    root = directory_for_connection(connection)
    entries = reviewed_index_entries(root) if root is not None else {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for company_ref, rows in requests.items():
        kinds = {kind for kind, _ in rows}
        try:
            products = final_surface_products(connection, mission, company_ref, kinds=kinds)
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            continue
        for product in products:
            key = (product["kind"], product["version_ref"])
            if key in rows:
                result[key] = reviewed_product_state(
                    connection, product, since=rows[key], entries=entries, now=now)
    return result


def _state_dir(connection: Any) -> Path | None:
    from .research_localization_store import directory_for_connection

    root = directory_for_connection(connection)
    return None if root is None else root.parent


__all__ = [
    "DEFAULT_DRAFT_AFTER_SECONDS", "DRAFT", "DRAFT_LABEL", "PENDING", "PENDING_BADGE", "PENDING_TEXT",
    "READY", "final_text_state", "language_policy", "publication_work_dir",
    "reviewed_product_state", "surface_states", "worker_product_state",
]
