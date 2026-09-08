"""Mission-driven source discovery (P9d-1 AlphaEngine, P9d-4a web search).

Three pieces sit here:

* ``DiscoveryPlan`` -- a human-authored, hash-bound manifest that says, per
  covered company, which search terms and which frozen search specs
  (document type, query template, look-back window, re-discovery cadence)
  the mission may run against one discovery source.  The Core never invents
  queries; every discovery record names the exact plan hash and spec it came
  from.  Schema 0.1 is the committed AlphaEngine plan; 0.2 adds
  ``source:web-search`` (Gemini ``search_web`` specs without a document type)
  and a plan-level trailing-24h call budget that binds every source.
* ``AlphaEngineSearchLauncher`` / ``WebSearchLauncher`` -- the writer-owned
  launchers for the out-of-process search children (the transport executor's
  SIGALRM watchdog needs a process main thread), mirroring the acquisition
  and SEC lane launchers: approved human governance record, single slot,
  owner-only tickets under ``<state>/discoveries/<ticket>/``.  Each launcher
  serves exactly one source; a plan for another source is refused.
* ``MissionSourceDiscoveryCoordinator`` -- what the controller tick calls,
  one instance per plan.  It settles finished children of its own source,
  launches at most one discovery and at most one budgeted document
  acquisition per call, and reports every skip with its
  reason (mission grant, cadence, budget, busy slot) instead of hiding it.

Nothing here writes Evidence, Claims or Theses.  Discovered documents that
Core acquires are raw connector authority; turning them into candidates is
the existing human-reviewed path.  Web search leaves only opaque URL refs
behind; the public-web fetch lane (P9d-4b) acquires the cited page's original
bytes through ``PublicWebFetchLauncher`` and the same settlement path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from .alphaengine_core_search import (
    SEARCH_DOCUMENT_TYPES,
    SearchConnectorGovernance,
    search_spec_hash,
    validate_search_spec,
)
from .bounded_alphaengine_probe import (
    MAX_CALLS_PER_WINDOW,
    count_recent_alphaengine_calls,
    document_in_authority,
)
from .coverage_mission import (
    DISCOVERY_SOURCES,
    CoverageMissionAuthority,
    CoverageMissionError,
    CoverageMissionNotFound,
)
from .public_web_connector import REDIRECT_PROXY_HOSTS
from .public_web_core_fetch import (
    PublicWebCoreFetchError,
    cited_hosts_from_discovery,
    count_recent_public_web_fetch_calls,
)
from .raw_spool import RawSpool
from .public_web_core_search import (
    WebSearchConnectorGovernance,
    count_recent_web_search_calls,
    public_web_urls_in_authority,
    validate_web_search_spec,
    web_search_spec_hash,
)
from .child_tickets import adopt_finished_child
from .store import DaltonStore, canonical_json, content_hash


DISCOVERY_PLAN_SCHEMA_VERSION = "0.1"
DISCOVERY_PLAN_SCHEMA_VERSION_V2 = "0.2"
# P9d-13: 0.3 adds a web-search acquisition policy (preferred / skipped hosts).
DISCOVERY_PLAN_SCHEMA_VERSION_V3 = "0.3"
# P10r: SEC filings index. Its "query" is an issuer and a form, not free
# text, so it carries its own spec shape rather than a query_template nobody
# would fill in.
DISCOVERY_PLAN_SCHEMA_VERSION_V4 = "0.4"
DISCOVERY_PLAN_SCHEMA_VERSIONS: tuple[str, ...] = (
    DISCOVERY_PLAN_SCHEMA_VERSION, DISCOVERY_PLAN_SCHEMA_VERSION_V2,
    DISCOVERY_PLAN_SCHEMA_VERSION_V3, DISCOVERY_PLAN_SCHEMA_VERSION_V4,
)
ALPHAENGINE_SOURCE_REF = "source:alphaengine"
WEB_SEARCH_SOURCE_REF = "source:web-search"
SEC_SOURCE_REF = "source:sec-edgar"
SEC_FILINGS_TICKET_PREFIX = "sec-filings-index"
# A 10-K window holds a couple of filings; the ceiling only has to be
# above that, and the adapter fails closed if the source exceeds it.
SEC_INDEX_LIMIT = 100
TICKET_SCHEMA_VERSION = "0.1"
TICKET_PREFIX = "alphaengine-discovery"
WEB_SEARCH_TICKET_PREFIX = "web-search-discovery"
LIVE_MODE_ARGS = ("--allow-network",)
# Hard ceiling on a plan's own trailing-24h call budget; a bigger number is
# an owner decision that belongs in governance, not in a plan file.
MAX_PLAN_CALLS_24H = 1000
# An acquisition child that failed (provider error, or orphaned by a deploy
# restart) is retried once this interval has passed; fresh documents are
# always acquired first.
ACQUISITION_RETRY_INTERVAL = timedelta(days=1)
_HUMAN_RE = re.compile(r"human:[A-Za-z0-9._-]+\Z")
_AUTOMATION_RE = re.compile(r"automation:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SPEC_REF_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_PLAN_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_ref", "source_ref", "companies", "specs",
    "content_hash",
})
_PLAN_FIELDS_V2 = _PLAN_FIELDS | frozenset({"budget"})
_PLAN_FIELDS_V3 = _PLAN_FIELDS_V2 | frozenset({"acquisition"})
_PLAN_FIELDS_V4 = _PLAN_FIELDS_V2
_BUDGET_FIELDS = frozenset({"max_calls_24h"})
_ACQUISITION_FIELDS = frozenset({"preferred_hosts", "skip_hosts"})
MAX_POLICY_HOSTS = 50
_HOST_RE = re.compile(
    r"(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+\Z"
)


def _plan_hosts(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise DiscoveryPlanError(f"{name} must be an array of hostnames")
    hosts: list[str] = []
    for item in value:
        if not isinstance(item, str) or _HOST_RE.fullmatch(item) is None:
            raise DiscoveryPlanError(f"{name} entries must be lowercase hostnames")
        if item in hosts:
            raise DiscoveryPlanError(f"{name} lists {item} twice")
        hosts.append(item)
    if len(hosts) > MAX_POLICY_HOSTS:
        raise DiscoveryPlanError(f"{name} may list at most {MAX_POLICY_HOSTS} hosts")
    return hosts


def _plan_acquisition(value: Any) -> dict[str, list[str]]:
    """Closed acquisition policy: which hosts the fetch lane prefers or never touches."""

    if not isinstance(value, Mapping) or set(value) != _ACQUISITION_FIELDS:
        raise DiscoveryPlanError("discovery plan acquisition must have exactly preferred_hosts and skip_hosts")
    preferred = _plan_hosts(value["preferred_hosts"], "acquisition.preferred_hosts")
    skipped = _plan_hosts(value["skip_hosts"], "acquisition.skip_hosts")
    overlap = sorted(set(preferred) & set(skipped))
    if overlap:
        raise DiscoveryPlanError(f"acquisition hosts cannot be both preferred and skipped: {overlap}")
    return {"preferred_hosts": preferred, "skip_hosts": skipped}
_COMPANY_FIELDS = frozenset({"search_terms"})
_SPEC_FIELDS = frozenset({
    "spec_ref", "document_type", "query_template", "lookback_days",
    "rediscovery_interval_days", "retry_interval_days",
})
# Web search has no library document type: one ranked page per query.
_WEB_SPEC_FIELDS = _SPEC_FIELDS - frozenset({"document_type"})
# The SEC index is asked for a form, not a phrase.
_SEC_SPEC_FIELDS = frozenset({
    "spec_ref", "form", "lookback_days", "rediscovery_interval_days",
    "retry_interval_days",
})
_SEC_COMPANY_FIELDS = frozenset({"cik"})
_CIK_RE = re.compile(r"[0-9]{10}\Z")


class DiscoveryPlanError(ValueError):
    """The discovery plan manifest is malformed or its hash does not bind."""


class DiscoveryLaunchError(RuntimeError):
    """Launcher configuration or filesystem failure."""


class DiscoveryLaunchRejected(ValueError):
    """The request was refused before any process was started."""


class DiscoveryLaunchConflict(RuntimeError):
    """Another discovery already holds the single slot."""


class DiscoveryTicketNotFound(LookupError):
    pass


# ---------------------------------------------------------------------------
# discovery plan
# ---------------------------------------------------------------------------
def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise DiscoveryPlanError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _plan_text(value: Any, name: str, *, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DiscoveryPlanError(f"{name} must be non-empty text (<= {maximum} chars)")
    return value.strip()


def _plan_time(value: Any) -> str:
    if not isinstance(value, str):
        raise DiscoveryPlanError("created_at must be RFC3339 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DiscoveryPlanError("created_at must be RFC3339 text") from exc
    if parsed.tzinfo is None:
        raise DiscoveryPlanError("created_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def validate_discovery_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("schema_version"), str):
        raise DiscoveryPlanError("discovery plan has an invalid closed shape")
    schema_version = value["schema_version"]
    if schema_version not in DISCOVERY_PLAN_SCHEMA_VERSIONS:
        raise DiscoveryPlanError("unsupported discovery plan schema_version")
    fields = {
        DISCOVERY_PLAN_SCHEMA_VERSION: _PLAN_FIELDS,
        DISCOVERY_PLAN_SCHEMA_VERSION_V2: _PLAN_FIELDS_V2,
        DISCOVERY_PLAN_SCHEMA_VERSION_V3: _PLAN_FIELDS_V3,
        DISCOVERY_PLAN_SCHEMA_VERSION_V4: _PLAN_FIELDS_V4,
    }[schema_version]
    if set(value) != fields:
        raise DiscoveryPlanError("discovery plan has an invalid closed shape")
    wire = json.loads(canonical_json(value))
    plan_id = _plan_text(wire["id"], "id")
    if not plan_id.startswith("discovery-plan:"):
        raise DiscoveryPlanError("discovery plan id must use the discovery-plan: namespace")
    mission_ref = _plan_text(wire["mission_ref"], "mission_ref")
    if not mission_ref.startswith("coverage-mission:"):
        raise DiscoveryPlanError("discovery plan mission_ref must use the coverage-mission: namespace")
    source_ref = wire["source_ref"]
    if schema_version == DISCOVERY_PLAN_SCHEMA_VERSION:
        if source_ref != ALPHAENGINE_SOURCE_REF:
            raise DiscoveryPlanError("discovery plan 0.1 source_ref must be source:alphaengine (P9d-1)")
    elif source_ref not in DISCOVERY_SOURCES:
        raise DiscoveryPlanError(
            f"discovery plan source_ref must be one of {sorted(DISCOVERY_SOURCES)}"
        )
    budget: dict[str, int] | None = None
    if schema_version != DISCOVERY_PLAN_SCHEMA_VERSION:
        raw_budget = wire["budget"]
        if not isinstance(raw_budget, Mapping) or set(raw_budget) != _BUDGET_FIELDS:
            raise DiscoveryPlanError("discovery plan budget must have exactly max_calls_24h")
        budget = {
            "max_calls_24h": _positive_int(
                raw_budget["max_calls_24h"], "budget.max_calls_24h", maximum=MAX_PLAN_CALLS_24H
            ),
        }
    acquisition: dict[str, list[str]] | None = None
    if schema_version == DISCOVERY_PLAN_SCHEMA_VERSION_V3:
        if source_ref != WEB_SEARCH_SOURCE_REF:
            raise DiscoveryPlanError("discovery plan 0.3 acquisition policy is only for source:web-search")
        acquisition = _plan_acquisition(wire["acquisition"])
    if (schema_version == DISCOVERY_PLAN_SCHEMA_VERSION_V4) != (source_ref == SEC_SOURCE_REF):
        raise DiscoveryPlanError(
            "discovery plan 0.4 is the SEC filings index shape and source:sec-edgar requires it"
        )
    if source_ref == SEC_SOURCE_REF:
        spec_fields = _SEC_SPEC_FIELDS
    elif source_ref == ALPHAENGINE_SOURCE_REF:
        spec_fields = _SPEC_FIELDS
    else:
        spec_fields = _WEB_SPEC_FIELDS
    companies = wire["companies"]
    if not isinstance(companies, Mapping) or not companies:
        raise DiscoveryPlanError("discovery plan companies must be a non-empty object")
    cleaned_companies: dict[str, dict[str, str]] = {}
    company_fields = (
        _SEC_COMPANY_FIELDS if source_ref == SEC_SOURCE_REF else _COMPANY_FIELDS
    )
    for company_ref in sorted(companies):
        entry = companies[company_ref]
        if not isinstance(entry, Mapping) or set(entry) != company_fields:
            raise DiscoveryPlanError(f"discovery plan company {company_ref} has an invalid shape")
        if source_ref == SEC_SOURCE_REF:
            # The index is asked for a CIK, and it must be the padded form the
            # SEC endpoint takes, not a number someone shortened by hand.
            cik = _plan_text(entry["cik"], "cik", maximum=10)
            if _CIK_RE.fullmatch(cik) is None:
                raise DiscoveryPlanError(
                    f"discovery plan company {company_ref} cik must be 10 digits"
                )
            cleaned_companies[_plan_text(company_ref, "company_ref")] = {"cik": cik}
            continue
        cleaned_companies[_plan_text(company_ref, "company_ref")] = {
            "search_terms": _plan_text(entry["search_terms"], "search_terms", maximum=120),
        }
    specs = wire["specs"]
    if not isinstance(specs, list) or not specs:
        raise DiscoveryPlanError("discovery plan specs must be a non-empty array")
    cleaned_specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in specs:
        if not isinstance(raw, Mapping) or set(raw) != spec_fields:
            raise DiscoveryPlanError("discovery plan spec has an invalid closed shape")
        spec_ref = _plan_text(raw["spec_ref"], "spec_ref", maximum=64)
        if _SPEC_REF_RE.fullmatch(spec_ref) is None or spec_ref in seen:
            raise DiscoveryPlanError("discovery plan spec_ref must be a unique kebab-case slug")
        seen.add(spec_ref)
        if "document_type" in spec_fields and raw["document_type"] not in SEARCH_DOCUMENT_TYPES:
            raise DiscoveryPlanError(f"discovery plan spec {spec_ref} document_type is not mapped")
        if "form" in spec_fields:
            form = _plan_text(raw["form"], "form", maximum=16)
            cleaned = {"spec_ref": spec_ref, "form": form}
        else:
            template = _plan_text(raw["query_template"], "query_template")
            if "{terms}" not in template or template.count("{") != 1 or template.count("}") != 1:
                raise DiscoveryPlanError(
                    f"discovery plan spec {spec_ref} query_template must contain exactly one {{terms}}"
                )
            cleaned = {"spec_ref": spec_ref, "query_template": template}
        cleaned |= {
            "lookback_days": _positive_int(raw["lookback_days"], "lookback_days", maximum=3650),
            "rediscovery_interval_days": _positive_int(
                raw["rediscovery_interval_days"], "rediscovery_interval_days", maximum=365
            ),
            "retry_interval_days": _positive_int(
                raw["retry_interval_days"], "retry_interval_days", maximum=365
            ),
        }
        if "document_type" in spec_fields:
            cleaned["document_type"] = raw["document_type"]
        cleaned_specs.append(cleaned)
    base = {
        "schema_version": schema_version,
        "id": plan_id,
        "created_at": _plan_time(wire["created_at"]),
        "mission_ref": mission_ref,
        "source_ref": source_ref,
        "companies": cleaned_companies,
        "specs": cleaned_specs,
    }
    if budget is not None:
        base["budget"] = budget
    if acquisition is not None:
        base["acquisition"] = acquisition
    expected = content_hash(base)
    if wire["content_hash"] != expected:
        raise DiscoveryPlanError("discovery plan content_hash does not bind its content")
    return {**base, "content_hash": expected}


def build_discovery_plan(
    *,
    plan_id: str,
    created_at: str,
    mission_ref: str,
    companies: Mapping[str, str],
    specs: Sequence[Mapping[str, Any]],
    source_ref: str = ALPHAENGINE_SOURCE_REF,
    max_calls_24h: int | None = None,
    acquisition: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Author a plan (hash appended) from search terms and spec rows.

    Without ``max_calls_24h`` an AlphaEngine plan is authored at schema 0.1
    (byte-identical to the committed P9d-1 plan); any other source, or an
    explicit plan budget, authors a 0.2 plan.  An ``acquisition`` policy
    (``preferred_hosts`` / ``skip_hosts``) authors a 0.3 web-search plan.
    """

    base: dict[str, Any] = {
        "schema_version": DISCOVERY_PLAN_SCHEMA_VERSION,
        "id": plan_id,
        "created_at": created_at,
        "mission_ref": mission_ref,
        "source_ref": source_ref,
        "companies": {ref: {"search_terms": terms} for ref, terms in companies.items()},
        "specs": [dict(spec) for spec in specs],
    }
    if source_ref != ALPHAENGINE_SOURCE_REF or max_calls_24h is not None:
        if max_calls_24h is None:
            raise DiscoveryPlanError("a 0.2 discovery plan requires max_calls_24h")
        base["schema_version"] = DISCOVERY_PLAN_SCHEMA_VERSION_V2
        base["budget"] = {"max_calls_24h": max_calls_24h}
    if acquisition is not None:
        base["schema_version"] = DISCOVERY_PLAN_SCHEMA_VERSION_V3
        base["acquisition"] = {
            "preferred_hosts": list(acquisition.get("preferred_hosts", ())),
            "skip_hosts": list(acquisition.get("skip_hosts", ())),
        }
    return validate_discovery_plan({**base, "content_hash": content_hash(base)})


def load_discovery_plan(path: str | Path) -> dict[str, Any]:
    return validate_discovery_plan(json.loads(Path(path).read_text(encoding="utf-8")))


def plan_spec(plan: Mapping[str, Any], spec_ref: str) -> dict[str, Any]:
    for spec in plan["specs"]:
        if spec["spec_ref"] == spec_ref:
            return dict(spec)
    raise DiscoveryPlanError(f"discovery plan has no spec {spec_ref}")


def build_discovery_parameters(
    plan: Mapping[str, Any], *, spec_ref: str, company_ref: str, as_of: date
) -> dict[str, Any]:
    """Deterministic search parameters for one plan spec and company.

    AlphaEngine plans compile to ``search_library`` specs; web search plans
    compile to ``search_web`` specs with an explicit date window.
    """

    spec = plan_spec(plan, spec_ref)
    company = plan["companies"].get(company_ref)
    if company is None:
        raise DiscoveryPlanError(f"discovery plan does not cover {company_ref}")
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise DiscoveryPlanError("as_of must be a calendar date")
    window_start = (as_of - timedelta(days=spec["lookback_days"])).isoformat()
    if plan["source_ref"] == SEC_SOURCE_REF:
        # P10s: the index takes an issuer and a form, so there is no phrase to
        # compile. The window is still the plan's, because the SEC submissions
        # feed refuses a window starting before the block it can answer from.
        from .sec_filings_index_core import validate_filings_index_spec

        return validate_filings_index_spec({
            "issuer": company["cik"],
            "form": spec["form"],
            "date_from": window_start,
            "date_to": as_of.isoformat(),
            "limit": SEC_INDEX_LIMIT,
        })
    query = spec["query_template"].replace("{terms}", company["search_terms"])
    if plan["source_ref"] == WEB_SEARCH_SOURCE_REF:
        return validate_web_search_spec({
            "query": query,
            "date_after": window_start,
            "date_before": as_of.isoformat(),
        })
    return validate_search_spec({
        "query": query,
        "filters": {
            "document_type": spec["document_type"],
            "date_from": window_start,
            "date_to": as_of.isoformat(),
        },
        "cursor": None,
    })


def discovery_query_hash(plan: Mapping[str, Any], parameters: Mapping[str, Any]) -> str:
    """Query hash of compiled parameters under the plan's source operation."""

    if plan["source_ref"] == SEC_SOURCE_REF:
        return content_hash({"operation": "list_filings", "parameters": dict(parameters)})
    if plan["source_ref"] == WEB_SEARCH_SOURCE_REF:
        return web_search_spec_hash(parameters)
    return search_spec_hash(parameters)


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------
def _wire_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _secure_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_owner_only(path: Path, value: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class _SearchLauncherBase:
    """Launch one source's search-discovery child against the writer's state dir.

    Subclasses freeze the source, the child module, the ticket prefix and how
    the governance record is loaded; the launch protocol (exact mission
    authorization, plan/governance re-validation, single slot, owner-only
    tickets) is shared.
    """

    SOURCE_REF: str = ""
    TICKET_PREFIX: str = ""
    CHILD_MODULE: str = ""
    LIVE_TRANSPORT_LABEL: str = "live"

    def __init__(
        self,
        *,
        state_dir: str | Path,
        governance_path: str | Path,
        plan_path: str | Path,
        mode_args: Sequence[str] = LIVE_MODE_ARGS,
        python_executable: str | None = None,
        clock: Callable[[], datetime] | None = None,
        spool_dir: str | Path | None = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.governance_path = Path(governance_path).expanduser().resolve()
        self.plan_path = Path(plan_path).expanduser().resolve()
        self.spool_dir = None if spool_dir is None else Path(spool_dir).expanduser().resolve()
        self.mode_args = tuple(str(item) for item in mode_args)
        self.python_executable = python_executable or sys.executable
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._current: tuple[str, subprocess.Popen[bytes]] | None = None
        self._ticket_re = re.compile(re.escape(self.TICKET_PREFIX) + r":[0-9a-f]{24}\Z")
        if not self.state_dir.is_dir():
            raise DiscoveryLaunchError("discovery state directory is missing")
        self.tickets_dir = _secure_dir(self.state_dir / "discoveries")

    @property
    def networked(self) -> bool:
        return "--allow-network" in self.mode_args

    def _load_governance_record(self) -> Any:
        raise NotImplementedError

    def _refuse_networked_launch(self) -> None:
        """Hook: refuse before spawning when live transport is unavailable."""

    def _extra_command_args(self) -> list[str]:
        return []

    def load_governance(self) -> Any:
        try:
            governance = self._load_governance_record()
        except FileNotFoundError as exc:
            raise DiscoveryLaunchRejected(
                "search connector governance record is missing; owner approval is required"
            ) from exc
        except Exception as exc:
            raise DiscoveryLaunchRejected(
                f"search connector governance record is invalid: {exc}"
            ) from exc
        if not governance.approved:
            raise DiscoveryLaunchRejected(
                "search connector governance record is not approved; owner approval is required"
            )
        if _HUMAN_RE.fullmatch(governance.approved_by) is None:
            raise DiscoveryLaunchRejected(
                "search connector governance record must be approved by a human principal"
            )
        return governance

    def load_plan(self) -> dict[str, Any]:
        try:
            plan = load_discovery_plan(self.plan_path)
        except FileNotFoundError as exc:
            raise DiscoveryLaunchRejected("discovery plan is missing") from exc
        except (DiscoveryPlanError, json.JSONDecodeError) as exc:
            raise DiscoveryLaunchRejected(f"discovery plan is invalid: {exc}") from exc
        if plan["source_ref"] != self.SOURCE_REF:
            raise DiscoveryLaunchRejected(
                f"discovery plan source {plan['source_ref']} is not served by this launcher"
            )
        return plan

    def _ticket_path(self, ticket_id: str) -> Path:
        return self.tickets_dir / ticket_id.split(":", 1)[1] / "ticket.json"

    def _command(
        self,
        *,
        company_ref: str,
        spec_ref: str,
        requested_by: str,
        mission_version_ref: str,
        mission_version_hash: str,
        as_of: str,
        ticket_dir: Path,
    ) -> list[str]:
        command = [
            self.python_executable, "-m", self.CHILD_MODULE,
            "--state-dir", str(self.state_dir),
            "--governance", str(self.governance_path),
            "--discovery-plan", str(self.plan_path),
            "--company-ref", company_ref,
            "--spec-ref", spec_ref,
            "--requested-by", requested_by,
            "--mission-version-ref", mission_version_ref,
            "--mission-version-hash", mission_version_hash,
            "--as-of", as_of,
            "--summary-dir", str(ticket_dir),
            "--quiet",
        ]
        if self.spool_dir is not None:
            command += ["--spool-dir", str(self.spool_dir)]
        command += self._extra_command_args()
        command += list(self.mode_args)
        return command

    def start(
        self,
        *,
        authorization: Mapping[str, Any],
        spec_ref: str,
        as_of: date | None = None,
    ) -> dict[str, Any]:
        """Spawn one discovery child bound to an exact mission authorization."""

        required = {
            "mission_version_ref", "mission_version_hash", "mission_ref", "company_ref",
            "ticker", "source_ref", "actor_ref", "requested_by", "scope",
            "max_alphaengine_calls_24h",
        }
        if not isinstance(authorization, Mapping) or set(authorization) != required:
            raise DiscoveryLaunchRejected("discovery launch requires an exact mission authorization")
        if authorization["scope"] != "source_discovery":
            raise DiscoveryLaunchRejected("discovery authorization scope is not source_discovery")
        requested_by = authorization["requested_by"]
        if not isinstance(requested_by, str) or (
            _HUMAN_RE.fullmatch(requested_by) is None
            and _AUTOMATION_RE.fullmatch(requested_by) is None
        ):
            raise DiscoveryLaunchRejected("discovery requester must use the human: or automation: namespace")
        if not isinstance(spec_ref, str) or _SPEC_REF_RE.fullmatch(spec_ref) is None:
            raise DiscoveryLaunchRejected("spec_ref must be a kebab-case slug")
        plan = self.load_plan()
        if plan["mission_ref"] != authorization["mission_ref"]:
            raise DiscoveryLaunchRejected("discovery plan does not belong to the authorized mission")
        if plan["source_ref"] != authorization["source_ref"]:
            raise DiscoveryLaunchRejected("discovery plan source differs from the authorization")
        if authorization["company_ref"] not in plan["companies"]:
            raise DiscoveryLaunchRejected("discovery plan does not cover the authorized company")
        try:
            plan_spec(plan, spec_ref)
        except DiscoveryPlanError as exc:
            raise DiscoveryLaunchRejected(str(exc)) from exc
        governance = self.load_governance()
        if self.networked:
            self._refuse_networked_launch()
        as_of_date = as_of or self.clock().date()
        if not isinstance(as_of_date, date) or isinstance(as_of_date, datetime):
            raise DiscoveryLaunchRejected("as_of must be a calendar date")
        with self._lock:
            if self._current is not None and self._current[1].poll() is None:
                raise DiscoveryLaunchConflict(f"discovery {self._current[0]} is still running")
            started_at = _wire_time(self.clock())
            digest = hashlib.sha256(
                canonical_json({
                    "authorization": dict(authorization), "spec_ref": spec_ref,
                    "as_of": as_of_date.isoformat(), "started_at": started_at,
                    "governance_hash": governance.content_hash,
                    "plan_hash": plan["content_hash"],
                }).encode("utf-8")
            ).hexdigest()[:24]
            ticket_id = f"{self.TICKET_PREFIX}:{digest}"
            ticket_dir = _secure_dir(self.tickets_dir / digest)
            command = self._command(
                company_ref=authorization["company_ref"], spec_ref=spec_ref,
                requested_by=requested_by,
                mission_version_ref=authorization["mission_version_ref"],
                mission_version_hash=authorization["mission_version_hash"],
                as_of=as_of_date.isoformat(), ticket_dir=ticket_dir,
            )
            log_path = ticket_dir / "run.log"
            log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.state_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            finally:
                os.close(log_fd)
            record = {
                "schema_version": TICKET_SCHEMA_VERSION,
                "id": ticket_id,
                "company_ref": authorization["company_ref"],
                "spec_ref": spec_ref,
                "requested_by": requested_by,
                "actor_ref": authorization["actor_ref"],
                "mission_version_ref": authorization["mission_version_ref"],
                "mission_version_hash": authorization["mission_version_hash"],
                "as_of": as_of_date.isoformat(),
                "governance_ref": governance.id,
                "governance_hash": governance.content_hash,
                "plan_ref": plan["id"],
                "plan_hash": plan["content_hash"],
                "transport": self.LIVE_TRANSPORT_LABEL if self.networked else "rehearsal",
                "started_at": started_at,
                "pid": process.pid,
                "status": "running",
                "exit_code": None,
                "completed_at": None,
            }
            _write_owner_only(self._ticket_path(ticket_id), record)
            self._current = (ticket_id, process)
            return dict(record)

    def status(self, ticket_ref: str) -> dict[str, Any]:
        if not isinstance(ticket_ref, str) or self._ticket_re.fullmatch(ticket_ref) is None:
            raise DiscoveryLaunchRejected(f"ticket_ref must be {self.TICKET_PREFIX}:<hex>")
        path = self._ticket_path(ticket_ref)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DiscoveryTicketNotFound(ticket_ref) from exc
        with self._lock:
            process = None
            if self._current is not None and self._current[0] == ticket_ref:
                process = self._current[1]
            if record["status"] == "running":
                if process is not None:
                    code = process.poll()
                    if code is not None:
                        record["exit_code"] = code
                        record["completed_at"] = _wire_time(self.clock())
                        record["status"] = "succeeded" if code == 0 else "failed"
                        _write_owner_only(path, record)
                elif not self._pid_alive(record.get("pid")):
                    # The writer restarted (or the child died) before this
                    # ticket was settled.  If the child left its own final
                    # summary, take that; the settle path re-verifies
                    # authority anyway.  Otherwise it is orphaned.
                    now = _wire_time(self.clock())
                    if not adopt_finished_child(record, path.with_name("summary.json"), now=now):
                        record["status"] = "orphaned"
                        record["completed_at"] = now
                    _write_owner_only(path, record)
        summary_path = path.with_name("summary.json")
        summary = None
        if record["status"] != "running" and summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        return {**record, "summary": summary}

    def running(self) -> bool:
        with self._lock:
            return self._current is not None and self._current[1].poll() is None

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait(self, timeout: float | None = None) -> int | None:
        with self._lock:
            current = self._current
        if current is None:
            return None
        return current[1].wait(timeout=timeout)

    def close(self) -> None:
        with self._lock:
            current, self._current = self._current, None
        if current is not None and current[1].poll() is None:
            current[1].terminate()
            try:
                current[1].wait(timeout=5)
            except subprocess.TimeoutExpired:
                current[1].kill()


class AlphaEngineSearchLauncher(_SearchLauncherBase):
    """Launch the AlphaEngine ``search_library`` child (P9d-1)."""

    SOURCE_REF = ALPHAENGINE_SOURCE_REF
    TICKET_PREFIX = TICKET_PREFIX
    CHILD_MODULE = "dalton_core.alphaengine_search_cli"
    LIVE_TRANSPORT_LABEL = "loopback-mcp"

    def __init__(self, *, mcp_endpoint: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.mcp_endpoint = mcp_endpoint

    def _load_governance_record(self) -> SearchConnectorGovernance:
        return SearchConnectorGovernance.load(self.governance_path)

    def _extra_command_args(self) -> list[str]:
        if self.networked and self.mcp_endpoint is not None:
            return ["--mcp-endpoint", self.mcp_endpoint]
        return []


class WebSearchLauncher(_SearchLauncherBase):
    """Launch the Gemini ``search_web`` child (P9d-4a).

    Only the rehearsal transport exists in this slice.  A networked launch
    would need the OpenClaw gateway to hand the child a host-owned
    ``web_search`` handle; until that bridge is wired the launcher refuses
    before any process starts, and the coordinator reports the reason.
    """

    SOURCE_REF = WEB_SEARCH_SOURCE_REF
    TICKET_PREFIX = WEB_SEARCH_TICKET_PREFIX
    CHILD_MODULE = "dalton_core.public_web_search_cli"
    LIVE_TRANSPORT_LABEL = "openclaw-search-broker"

    def __init__(
        self,
        *,
        broker_socket: str | Path | None = None,
        broker_auth_key: str | Path | None = None,
        broker_client_id: str = "client:dalton-core",
        broker_profile_id: str = "profile:web-search",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.broker_socket = None if broker_socket is None else str(Path(broker_socket).expanduser())
        self.broker_auth_key = None if broker_auth_key is None else str(Path(broker_auth_key).expanduser())
        self.broker_client_id = broker_client_id
        self.broker_profile_id = broker_profile_id

    def _load_governance_record(self) -> WebSearchConnectorGovernance:
        return WebSearchConnectorGovernance.load(self.governance_path)

    def _refuse_networked_launch(self) -> None:
        # P9d-4d: a networked search goes through the host-owned OpenClaw
        # search broker.  Without its socket and key there is nothing to call,
        # so refuse before spawning rather than spending a launch.
        if not self.broker_socket or not self.broker_auth_key:
            raise DiscoveryLaunchRejected(
                "web search broker is not configured: the launcher needs the host "
                "broker socket and its owner-only key"
            )

    def _extra_command_args(self) -> list[str]:
        if not self.networked or not self.broker_socket or not self.broker_auth_key:
            return []
        return [
            "--broker-socket", self.broker_socket,
            "--broker-auth-key", self.broker_auth_key,
            "--broker-client-id", self.broker_client_id,
            "--broker-profile-id", self.broker_profile_id,
        ]


# ---------------------------------------------------------------------------
# controller-tick coordinator
# ---------------------------------------------------------------------------
def alphaengine_calls_remaining(
    connection: Any, *, mission_cap: int, owner_cap: int = MAX_CALLS_PER_WINDOW,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """Trailing-24h AlphaEngine calls (search + document pages) against the tighter cap."""

    spent = count_recent_alphaengine_calls(connection, as_of=as_of)
    cap = min(int(mission_cap), int(owner_cap))
    return {"spent": spent, "cap": cap, "remaining": max(0, cap - spent)}


def _parse_wire_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class SecFilingsIndexLauncher(_SearchLauncherBase):
    """Launch the SEC ``list_filings`` discovery child (P10u).

    The index is plain public HTTPS with no broker and no credential, so the
    shared launch protocol needs nothing added: only the source, the child
    module and how the approval is loaded differ.
    """

    SOURCE_REF = SEC_SOURCE_REF
    TICKET_PREFIX = SEC_FILINGS_TICKET_PREFIX
    CHILD_MODULE = "dalton_core.sec_filings_index_cli"
    LIVE_TRANSPORT_LABEL = "data.sec.gov"

    def __init__(self, *, user_agent: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.user_agent = user_agent

    def _load_governance_record(self) -> Any:
        from .connector_governance import load_connector_governance

        return load_connector_governance(self.governance_path)

    def _extra_command_args(self) -> list[str]:
        return [] if self.user_agent is None else ["--user-agent", self.user_agent]


class MissionSourceDiscoveryCoordinator:
    """Advance one plan's source discovery by at most one search + one acquisition.

    The coordinator only ever touches dispatches and documents of its own
    plan's source, so an AlphaEngine coordinator never settles a web search
    ticket (or the reverse) and never hands a URL ref to the AlphaEngine
    acquisition launcher.
    """

    def __init__(
        self,
        *,
        store: DaltonStore,
        missions: CoverageMissionAuthority,
        plan: Mapping[str, Any],
        search_launcher: Any | None,
        acquisition_launcher: Any | None,
        clock: Callable[[], datetime] | None = None,
        owner_call_cap: int = MAX_CALLS_PER_WINDOW,
        spool_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.missions = missions
        self.plan = validate_discovery_plan(plan)
        self.source_ref = self.plan["source_ref"]
        self.search_launcher = search_launcher
        self.acquisition_launcher = acquisition_launcher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_call_cap = int(owner_call_cap)
        # P9d-13: the connector spool, read only, is the sole route from a
        # public-web ref back to its host for rows recorded before the ledger
        # carried one.  Optional: without it the backfill reports "no_spool".
        self.spool_dir = None if spool_dir is None else Path(spool_dir)
        self._spool: RawSpool | None = None
        policy = self.plan.get("acquisition") or {}
        self.preferred_hosts: tuple[str, ...] = tuple(policy.get("preferred_hosts", ()))
        # The provider's redirect proxies are held with the plan's skip list:
        # not policy but a physical fact, the transport refuses to follow one
        # out, so fetching such a row could only ever fail.
        self.skip_hosts: tuple[str, ...] = tuple(dict.fromkeys(
            [*policy.get("skip_hosts", ()), *sorted(REDIRECT_PROXY_HOSTS)]
        )) if self.source_ref == WEB_SEARCH_SOURCE_REF else tuple(policy.get("skip_hosts", ()))

    # -- P9d-12/13 maintenance -------------------------------------------------
    def carry_forward(self) -> list[dict[str, Any]]:
        """Re-register this source's documents stranded under a superseded mission version."""

        try:
            return self.missions.carry_forward_superseded_documents(
                self.plan["mission_ref"], source_ref=self.source_ref
            )
        except Exception as exc:  # maintenance must never take the tick down
            return [{"status": "error", "reason": f"{type(exc).__name__}: {exc}"}]

    def settle_already_held(self) -> list[dict[str, Any]]:
        """Documents search found already in authority owe a review, not a fetch."""

        settled: list[dict[str, Any]] = []
        for document in self.missions.already_held_documents(source_ref=self.source_ref):
            entry: dict[str, Any] = {
                "record_id": document["record_id"], "document_ref": document["document_ref"],
            }
            if not self._document_in_authority(document["document_ref"]):
                # The ledger says held but this source's authority does not
                # agree; report it rather than queue a review for nothing.
                settled.append({**entry, "status": "not_in_authority"})
                continue
            result = self.missions.settle_document_already_held(document["record_id"])
            entry["status"] = result["status"]
            try:
                review = self.missions.register_document_review(
                    document["record_id"],
                    requested_by=self.missions.mission(
                        document["mission_version_ref"]
                    )["autonomy"]["automation_principal"],
                )
                entry["review_status"], entry["review_id"] = review["status"], review["review_id"]
            except CoverageMissionError as exc:
                entry["review_status"] = f"not_registered:{type(exc).__name__}"
            settled.append(entry)
        return settled

    def backfill_hosts(self, *, limit: int = 25) -> dict[str, Any]:
        """Fill ``host`` for pre-P9d-13 web rows from each row's exact discovery envelope."""

        if self.source_ref != WEB_SEARCH_SOURCE_REF:
            return {"status": "not_applicable"}
        rows = self.missions.documents_without_host(source_ref=self.source_ref, limit=limit)
        if not rows:
            return {"status": "complete", "filled": 0}
        if self.spool_dir is None:
            return {"status": "no_spool", "pending": len(rows)}
        if self._spool is None:
            self._spool = RawSpool(str(self.spool_dir), max_total_bytes=1_000_000_000)
        filled = 0
        failures: list[dict[str, str]] = []
        hosts_by_envelope: dict[str, dict[str, str]] = {}
        for document in rows:
            try:
                discovery = self.missions.discovery_record(document["discovery_ref"])
                envelope_ref = discovery["source_envelope_ref"]
                if envelope_ref not in hosts_by_envelope:
                    hosts_by_envelope[envelope_ref] = cited_hosts_from_discovery(
                        self.store.connection, self._spool, source_envelope_ref=envelope_ref,
                    )
                host = hosts_by_envelope[envelope_ref].get(document["document_ref"])
                if not host:
                    raise PublicWebCoreFetchError("url_ref is not cited by the discovery envelope")
                self.missions.set_document_host(document["record_id"], host)
                filled += 1
            except Exception as exc:  # one bad row must not stall the rest
                failures.append({"record_id": document["record_id"], "reason": f"{type(exc).__name__}: {exc}"})
        return {"status": "filled" if filled else "stalled", "filled": filled, "failures": failures}

    # -- settlement ----------------------------------------------------------
    def settle_dispatches(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        if self.search_launcher is None:
            return settled
        for dispatch in self.missions.open_discovery_dispatches(source_ref=self.source_ref):
            try:
                ticket = self.search_launcher.status(dispatch["ticket_ref"])
            except DiscoveryTicketNotFound:
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason="ticket is missing"
                )
                settled.append({"dispatch_ref": dispatch["dispatch_id"], "status": result["status"]})
                continue
            if ticket["status"] == "running":
                continue
            summary = ticket.get("summary") or {}
            if ticket["status"] == "succeeded" and summary.get("discovery_ref"):
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="succeeded"
                )
            else:
                reason = (
                    summary.get("failure_reason")
                    or f"child ended {ticket['status']} (exit {ticket.get('exit_code')})"
                )
                result = self.missions.settle_discovery_dispatch(
                    dispatch["dispatch_id"], status="failed", reason=str(reason)[:500]
                )
            settled.append({
                "dispatch_ref": dispatch["dispatch_id"], "status": result["status"],
                "ticket_ref": dispatch["ticket_ref"],
                "discovery_ref": summary.get("discovery_ref"),
                "new_document_count": summary.get("new_document_count"),
            })
        return settled

    def settle_documents(self) -> list[dict[str, Any]]:
        settled: list[dict[str, Any]] = []
        if self.acquisition_launcher is None:
            return settled
        for document in self.missions.launched_discovered_documents(source_ref=self.source_ref):
            try:
                ticket = self.acquisition_launcher.status(document["ticket_ref"])
            except LookupError:
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed", reason="ticket is missing"
                )
                settled.append({"record_id": document["record_id"], "status": result["status"]})
                continue
            if ticket.get("status") == "running":
                continue
            review_status: str | None = None
            review_id: str | None = None
            if ticket.get("status") == "succeeded" and self._document_in_authority(
                document["document_ref"]
            ):
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquired"
                )
                # P9d-2: every acquired document enters the human extraction
                # queue.  Registration re-derives the mission grant, so a
                # superseded mission simply leaves the document without a
                # review; the tick result reports the reason.
                try:
                    review = self.missions.register_document_review(
                        document["record_id"],
                        requested_by=self.missions.mission(
                            document["mission_version_ref"]
                        )["autonomy"]["automation_principal"],
                    )
                    review_status, review_id = review["status"], review["review_id"]
                except CoverageMissionError as exc:
                    review_status = f"not_registered:{type(exc).__name__}"
            else:
                # P9d-9: prefer the child's own failure_reason, the way
                # settle_dispatches already does for search.  The exit code
                # alone cannot distinguish a host that refuses every automated
                # client from a transient fault, and that difference decides
                # whether re-queueing the URL is worth a governed call.
                summary = ticket.get("summary") or {}
                reason = (
                    "acquisition succeeded but the document is not in authority"
                    if ticket.get("status") == "succeeded"
                    else (
                        summary.get("failure_reason")
                        or f"acquisition ended {ticket.get('status')} (exit {ticket.get('exit_code')})"
                    )
                )
                result = self.missions.settle_discovered_document(
                    document["record_id"], status="acquisition_failed", reason=str(reason)[:500]
                )
            entry = {
                "record_id": document["record_id"], "document_ref": document["document_ref"],
                "status": result["status"], "ticket_ref": document["ticket_ref"],
            }
            if review_status is not None:
                entry["review_status"] = review_status
                if review_id is not None:
                    entry["review_id"] = review_id
            settled.append(entry)
        return settled

    def _document_in_authority(self, document_ref: str) -> bool:
        """Core holds the document's bytes through this source's own acquisition op."""

        if self.source_ref == WEB_SEARCH_SOURCE_REF:
            return bool(public_web_urls_in_authority(self.store.connection, [document_ref]))
        return document_in_authority(self.store.connection, document_ref)

    # -- discovery launch ----------------------------------------------------
    def _cadence_block(self, mission_version_ref: str, company_ref: str, spec: Mapping[str, Any]) -> str | None:
        latest = self.missions.discovery_dispatches(
            mission_version_ref, company_ref=company_ref, spec_ref=spec["spec_ref"], limit=1
        )
        if not latest:
            return None
        row = latest[0]
        age = self.clock() - _parse_wire_time(row["created_at"])
        if row["status"] == "launched":
            return "previous discovery still open"
        if row["status"] == "succeeded":
            if age < timedelta(days=spec["rediscovery_interval_days"]):
                return f"rediscovered {age.days}d ago; interval {spec['rediscovery_interval_days']}d"
            return None
        if age < timedelta(days=spec["retry_interval_days"]):
            return f"last attempt {row['status']} {age.days}d ago; retry interval {spec['retry_interval_days']}d"
        return None

    def _reserved_calls(self) -> int:
        """Children launched but not yet settled may not have recorded their call yet."""

        open_dispatches = len(self.missions.open_discovery_dispatches(
            limit=100, source_ref=self.source_ref,
        ))
        open_documents = len(self.missions.launched_discovered_documents(
            limit=100, source_ref=self.source_ref,
        ))
        return open_dispatches + open_documents

    def _budget(self, mission_cap: int) -> dict[str, int]:
        """Remaining trailing-24h calls for this plan's source.

        SEC filings index: the plan budget alone, because the reads are free
        public HTTPS against a rate-limited endpoint rather than metered calls.

        AlphaEngine: the tighter of the mission grant and the owner cap
        (search + document pages share one window), further tightened by a
        0.2 plan budget.  Web search: the plan budget alone bounds Gemini
        calls -- the mission body has no web-search field, and the host owns
        the bill, so the human-authored, hash-bound plan is the cap.
        """

        plan_cap = (self.plan.get("budget") or {}).get("max_calls_24h")
        if self.source_ref == SEC_SOURCE_REF:
            # P10u: SEC index reads are free public HTTPS and are not
            # AlphaEngine calls. Falling through to the AlphaEngine window
            # gated them behind an unrelated source's exhausted quota, which
            # is what the first live tick reported. The plan's own budget is
            # the cap, as it is for web search.
            from .sec_filings_index_core import count_recent_index_calls

            spent = count_recent_index_calls(self.store.connection, as_of=self.clock())
            cap = int(plan_cap)
            budget = {"spent": spent, "cap": cap, "remaining": max(0, cap - spent)}
        elif self.source_ref == WEB_SEARCH_SOURCE_REF:
            # Searches and page fetches share the plan window, as AlphaEngine
            # searches and document pages share the mission window.
            spent = count_recent_web_search_calls(
                self.store.connection, as_of=self.clock()
            ) + count_recent_public_web_fetch_calls(self.store.connection, as_of=self.clock())
            cap = int(plan_cap)
            budget = {"spent": spent, "cap": cap, "remaining": max(0, cap - spent)}
        else:
            owner_cap = self.owner_call_cap if plan_cap is None else min(self.owner_call_cap, int(plan_cap))
            budget = alphaengine_calls_remaining(
                self.store.connection, mission_cap=mission_cap,
                owner_cap=owner_cap, as_of=self.clock(),
            )
        reserved = self._reserved_calls()
        budget["reserved"] = reserved
        budget["remaining"] = max(0, budget["remaining"] - reserved)
        return budget

    def launch_discovery(self) -> dict[str, Any]:
        if self.search_launcher is None:
            return {"status": "unconfigured", "reason": "search launcher is not configured"}
        try:
            mission = self.missions.active_mission(self.plan["mission_ref"])
        except CoverageMissionNotFound:
            return {"status": "no_active_mission", "mission_ref": self.plan["mission_ref"]}
        if self.search_launcher.running():
            return {"status": "busy", "reason": "a discovery child is still running"}
        skipped: list[dict[str, Any]] = []
        for member in mission["universe"]:
            company_ref = member["company_ref"]
            if company_ref not in self.plan["companies"]:
                skipped.append({"company_ref": company_ref, "reason": "not in discovery plan"})
                continue
            for spec in self.plan["specs"]:
                block = self._cadence_block(mission["id"], company_ref, spec)
                if block is not None:
                    skipped.append({
                        "company_ref": company_ref, "spec_ref": spec["spec_ref"], "reason": block,
                    })
                    continue
                try:
                    authorization = self.missions.authorize_source_discovery(
                        company_ref=company_ref,
                        source_ref=self.plan["source_ref"],
                        requested_by=mission["autonomy"]["automation_principal"],
                        mission_version_ref=mission["id"],
                        mission_version_hash=mission["content_hash"],
                    )
                except CoverageMissionError as exc:
                    # The grant is mission-wide; one refusal means every
                    # candidate would be refused.  Report and stop.
                    return {
                        "status": "not_authorized",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "mission_version_ref": mission["id"],
                        "skipped": skipped,
                    }
                budget = self._budget(authorization["max_alphaengine_calls_24h"])
                if budget["remaining"] < 1:
                    return {"status": "budget_exhausted", "budget": budget, "skipped": skipped}
                parameters = build_discovery_parameters(
                    self.plan, spec_ref=spec["spec_ref"], company_ref=company_ref,
                    as_of=self.clock().date(),
                )
                try:
                    ticket = self.search_launcher.start(
                        authorization=authorization, spec_ref=spec["spec_ref"],
                        as_of=self.clock().date(),
                    )
                except DiscoveryLaunchConflict as exc:
                    return {"status": "busy", "reason": str(exc), "skipped": skipped}
                except DiscoveryLaunchRejected as exc:
                    return {
                        "status": "rejected", "reason": f"{type(exc).__name__}: {exc}",
                        "company_ref": company_ref, "spec_ref": spec["spec_ref"],
                        "skipped": skipped,
                    }
                dispatch = self.missions.record_discovery_dispatch(
                    authorization=authorization,
                    discovery_plan_ref=self.plan["id"],
                    discovery_plan_hash=self.plan["content_hash"],
                    spec_ref=spec["spec_ref"],
                    query_hash=discovery_query_hash(self.plan, parameters),
                    ticket_ref=ticket["id"],
                )
                return {
                    "status": "launched",
                    "dispatch_ref": dispatch["dispatch_id"],
                    "ticket_ref": ticket["id"],
                    "company_ref": company_ref,
                    "spec_ref": spec["spec_ref"],
                    "budget": budget,
                    "skipped": skipped,
                }
        return {"status": "idle", "skipped": skipped}

    # -- document acquisition ------------------------------------------------
    def _stage_needs(self) -> tuple[list[dict[str, str]], str | None]:
        """P10a: what the missions still need from this source, most important first.

        A governed call should buy the P0 company's missing transcript before
        the P2 company's twentieth broker report.  A failure here costs only
        the ordering, never the acquisition, but it is reported rather than
        swallowed: a silently empty order looks exactly like no gaps at all.
        """

        try:
            from .mission_stage import MissionStageDriver, planned_spec_refs

            driver = MissionStageDriver(
                self.missions, planned_specs=planned_spec_refs([self.plan])
            )
            return ([
                {"company_ref": need["company_ref"], "spec_ref": need["spec_ref"]}
                for need in driver.needs(source_ref=self.source_ref)
            ], None)
        except Exception as exc:  # noqa: BLE001 - ordering is an optimisation, not a gate
            return ([], f"{type(exc).__name__}: {exc}")

    def launch_acquisition(self) -> dict[str, Any]:
        if self.acquisition_launcher is None:
            queued = self.missions.next_discovered_document(source_ref=self.source_ref)
            return {
                "status": "unconfigured",
                "reason": (
                    "public-web fetch launcher is not configured; discovered URLs stay queued"
                    if self.source_ref == WEB_SEARCH_SOURCE_REF
                    else "acquisition launcher is not configured"
                ),
                "queued": queued is not None,
            }
        if self.missions.launched_discovered_documents(limit=1, source_ref=self.source_ref):
            return {"status": "busy", "reason": "a discovered-document acquisition is still open"}
        retry = False
        needs, needs_error = self._stage_needs()
        stage_order = {
            "count": len(needs), "error": needs_error,
            "first": needs[0] if needs else None,
        }
        document = self.missions.next_discovered_document(
            source_ref=self.source_ref,
            preferred_hosts=self.preferred_hosts, skip_hosts=self.skip_hosts,
            preferred_needs=needs,
        )
        if document is not None and self._document_in_authority(document["document_ref"]):
            # A human acquisition already put these bytes into authority; settle
            # the row and queue the review instead of paying for them twice.
            result = self.missions.settle_document_already_held(document["record_id"])
            entry: dict[str, Any] = {
                "status": "already_in_authority", "record_id": document["record_id"],
                "document_ref": document["document_ref"], "settled_status": result["status"],
            }
            try:
                review = self.missions.register_document_review(
                    document["record_id"],
                    requested_by=self.missions.mission(
                        document["mission_version_ref"]
                    )["autonomy"]["automation_principal"],
                )
                entry["review_status"], entry["review_id"] = review["status"], review["review_id"]
            except CoverageMissionError as exc:
                entry["review_status"] = f"not_registered:{type(exc).__name__}"
            return entry
        if document is None:
            # No fresh documents: retry the oldest acquisition failure whose
            # interval has passed (e.g. a child orphaned by a deploy restart).
            document = self.missions.retryable_failed_document(
                older_than=ACQUISITION_RETRY_INTERVAL, as_of=self.clock(),
                source_ref=self.source_ref, skip_hosts=self.skip_hosts,
            )
            retry = document is not None
        if document is None:
            idle: dict[str, Any] = {"status": "idle", "stage_order": stage_order}
            if self.skip_hosts:
                idle["held_by_skip_hosts"] = self.missions.discovered_documents_held_by_skip(
                    source_ref=self.source_ref, skip_hosts=self.skip_hosts
                )
            return idle
        try:
            authorization = self.missions.authorize_source_discovery(
                company_ref=document["company_ref"],
                source_ref=document["source_ref"],
                requested_by=self.missions.mission(document["mission_version_ref"])["autonomy"]["automation_principal"],
                mission_version_ref=document["mission_version_ref"],
            )
        except CoverageMissionError as exc:
            return {
                "status": "not_authorized", "record_id": document["record_id"],
                "document_ref": document["document_ref"],
                "reason": f"{type(exc).__name__}: {exc}",
            }
        budget = self._budget(authorization["max_alphaengine_calls_24h"])
        if budget["remaining"] < 1:
            return {"status": "budget_exhausted", "budget": budget,
                    "document_ref": document["document_ref"], "stage_order": stage_order}
        try:
            ticket = self.acquisition_launcher.start_bounded_probe(
                document_ref=document["document_ref"],
                caller_ref=authorization["actor_ref"],
            )
        except Exception as exc:  # launcher conflict / rejection: report, keep the row
            name = type(exc).__name__
            status = "busy" if name.endswith("Conflict") else "rejected"
            return {
                "status": status, "reason": f"{name}: {exc}",
                "document_ref": document["document_ref"], "record_id": document["record_id"],
            }
        if retry:
            self.missions.mark_failed_document_retry_launched(document["record_id"], ticket["id"])
        else:
            self.missions.mark_discovered_document_launched(document["record_id"], ticket["id"])
        return {
            "status": "launched", "record_id": document["record_id"],
            "document_ref": document["document_ref"], "ticket_ref": ticket["id"],
            "retry": retry, "budget": budget,
        }

    def dispatch_once(self) -> dict[str, Any]:
        settled_dispatches = self.settle_dispatches()
        settled_documents = self.settle_documents()
        # P9d-12/13 maintenance, all before any new spend: documents stranded
        # by a mission version change come back under the current grant,
        # pre-ledger rows learn their host, and documents already held owe a
        # review rather than a fetch.
        carried_forward = self.carry_forward()
        host_backfill = self.backfill_hosts()
        already_held = self.settle_already_held()
        review_backfill = self.missions.backfill_document_reviews(self.plan["mission_ref"])
        # Acquiring an already-discovered document comes before spending the
        # shared budget on a new search: known gaps first, then new ones.
        acquisition = self.launch_acquisition()
        discovery = self.launch_discovery()
        active = discovery.get("status") == "launched" or acquisition.get("status") == "launched"
        return {
            "status": "launched" if active else "idle",
            "source_ref": self.source_ref,
            "plan_ref": self.plan["id"],
            "plan_hash": self.plan["content_hash"],
            "settled_dispatches": settled_dispatches,
            "settled_documents": settled_documents,
            "carried_forward": carried_forward,
            "host_backfill": host_backfill,
            "already_held": already_held,
            "review_backfill": review_backfill,
            "discovery": discovery,
            "acquisition": acquisition,
        }


__all__ = [
    "ALPHAENGINE_SOURCE_REF",
    "AlphaEngineSearchLauncher",
    "DISCOVERY_PLAN_SCHEMA_VERSION",
    "DISCOVERY_PLAN_SCHEMA_VERSION_V2",
    "DISCOVERY_PLAN_SCHEMA_VERSION_V3",
    "DISCOVERY_PLAN_SCHEMA_VERSIONS",
    "DiscoveryLaunchConflict",
    "DiscoveryLaunchError",
    "DiscoveryLaunchRejected",
    "DiscoveryPlanError",
    "DiscoveryTicketNotFound",
    "LIVE_MODE_ARGS",
    "MAX_PLAN_CALLS_24H",
    "MissionSourceDiscoveryCoordinator",
    "WEB_SEARCH_SOURCE_REF",
    "WEB_SEARCH_TICKET_PREFIX",
    "WebSearchLauncher",
    "alphaengine_calls_remaining",
    "build_discovery_parameters",
    "build_discovery_plan",
    "discovery_query_hash",
    "load_discovery_plan",
    "plan_spec",
    "validate_discovery_plan",
]
