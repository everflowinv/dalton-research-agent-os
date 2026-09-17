"""Why a new workspace's writer runs fewer lanes than the legacy Core, in one table.

A lane turns itself on from its own ``argv_fragment``: it looks at the state
directory, and if the files it needs are there it contributes the flags that
make the writer build its launcher.  That rule is right, and it is why a fresh
workspace comes up with a third of the legacy environment's lanes -- the files
simply are not there.  What was missing was somewhere that says *which* files,
*where they come from*, and *who has to decide*, because those three answers
were spread across ``deploy/macos/install.sh``, nine lane modules and the
owner's memory.

This module is that place.  Every lane input falls into exactly one of four
provenances, and the distinction is the whole point:

``host``
    A fact about this machine that every environment on it may share: the
    OpenClaw workspace the wiki lives in, the market-digest output directory,
    the two crowd host tools, the credential grants the host issued.  Staging
    these is a link, never a copy, because the host keeps writing to them.

``packaged``
    A contract this repository ships and the owner already approved when they
    approved the packaged connector: the yfinance, SEC-ownership, HKEX,
    prior-research and crowd governance records.  A workspace builds its own
    copy from ``build_governance_record`` rather than copying bytes out of
    another environment's state, so the record's ``approved_by`` names this
    workspace's owner and nothing is inherited.  ``SEED_FILES`` belongs here
    too: an empty, owner-editable default whose presence is what a flag needs.

``mission``
    A plan whose content is this mission's universe: the feed discovery plan,
    the Guidepoint discovery plan, the crowd source map.  ``research-foundation
    .json`` has always listed these under ``mission_generated_files``, but only
    the SEC/AlphaEngine/web-search discovery plans were ever actually generated
    at first publish -- which is why three lanes that had approved records sat
    ``held`` with "no lane on this writer".  They are generated here from the
    published mission instead of being copied from an industry that is not this
    workspace's.

``owner``
    Something nobody can derive: an X handle, an employer-review slug, a
    mission source grant that was published without a source in it.  These are
    *reported* with the reason.  A parity tool that silently skipped them would
    be telling the owner the lane is impossible when in fact it is one decision
    away.

The audit is read-only on purpose: it is the thing an owner runs against a live
environment, so it opens the mission database read-only and never touches the
writer, its socket or its plist.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "dalton-workspace-lane-parity-0.1"

#: Mission-generated plan filenames.  A lane looks for these *before* the
#: packaged ``us-it-services`` file, so a workspace that generated its own plan
#: runs on its own universe and a legacy Core keeps running on the file it has.
MISSION_FEED_PLAN_NAME = "mission-feeds-v1.json"
MISSION_GUIDEPOINT_PLAN_NAME = "mission-guidepoint-v1.json"
MISSION_CROWD_MAP_NAME = "mission-crowd-sources-v1.json"


class LaneParityError(RuntimeError):
    """A parity plan cannot be produced or applied."""


# -- host sources -------------------------------------------------------------


@dataclass(frozen=True)
class HostSource:
    """One host-level path a lane needs, and where this machine keeps it.

    ``relative`` is where it lands inside a state directory; that name is the
    lane's contract and is identical in every environment.  ``environment`` is
    the variable ``deploy/macos/install.sh`` already documents, so an owner who
    set it for the legacy install does not have to say it twice.
    """

    relative: str
    label: str
    environment: str | None = None
    openclaw_relative: str | None = None
    executable: bool = False
    #: A directory only counts as this source when it already holds one of
    #: these. The company-wiki corpus root is the whole OpenClaw workspace, and
    #: an OpenClaw install with no wiki index would otherwise resolve to a root
    #: whose index file the lane then cannot find -- a lane switched on with
    #: nothing to read, which is the failure the all-or-nothing rule exists to
    #: prevent. ``install.sh`` makes the same check.
    requires_any: tuple[str, ...] = ()


#: Keyed by the path a state directory holds it at.  ``openclaw_relative`` is
#: resolved against the OpenClaw workspace, which is itself the company-wiki
#: corpus root -- the wiki index rows carry paths relative to that root, so the
#: link has to be the workspace itself and not a directory inside it.
HOST_SOURCES: tuple[HostSource, ...] = (
    HostSource("feeds/company-wiki", "公司知识库语料（OpenClaw 工作区，且其中要有 wiki 索引）",
               environment="DALTON_OPENCLAW_WORKSPACE", openclaw_relative=".",
               requires_any=("wiki/vectors.db", "wiki-index.sqlite")),
    HostSource("feeds/market-digest-output", "卖方销售快报摘要目录",
               openclaw_relative="skills/market-digest/output"),
    HostSource("feeds/prior-research", "本机既有研究资料库",
               environment="DALTON_PRIOR_RESEARCH_DIR"),
    HostSource("host-tools/xueqiu", "雪球取数工具", executable=True),
    HostSource("host-tools/xueqiu-hot-rank", "雪球热榜工具", executable=True),
    HostSource("host-tools/xreach", "X/Twitter 取数工具", executable=True),
    HostSource("credential-grants/xueqiu.json", "雪球凭证授权书"),
    HostSource("credential-grants/xreach.json", "X/Twitter 凭证授权书"),
)


def default_openclaw_workspace(environ: Mapping[str, str] | None = None,
                               home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    declared = env.get("DALTON_OPENCLAW_WORKSPACE")
    if declared:
        return Path(declared).expanduser()
    return (Path.home() if home is None else Path(home)) / ".openclaw" / "workspace"


def resolve_host_sources(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    source_state_dir: str | Path | None = None,
) -> dict[str, Path]:
    """Where each host input actually is on this machine, or absent.

    Three places are tried in order, and the order is the order of authority:
    what the owner declared in the environment, what another environment on
    this machine already resolved (its own link target, which the owner
    accepted when they installed it), and finally the packaged default under
    the OpenClaw workspace.  Absent everywhere means absent: a lane whose host
    input this machine does not have is reported, not invented.
    """

    env = os.environ if environ is None else environ
    workspace_root = default_openclaw_workspace(env, home)
    source_state = (Path(source_state_dir).expanduser().resolve()
                    if source_state_dir is not None else None)
    resolved: dict[str, Path] = {}
    for source in HOST_SOURCES:
        candidates: list[Path] = []
        if source.environment:
            declared = env.get(source.environment)
            if declared:
                candidates.append(Path(declared).expanduser())
        if source_state is not None:
            candidates.append(source_state / source.relative)
        if source.openclaw_relative is not None:
            candidates.append(workspace_root / source.openclaw_relative)
        for candidate in candidates:
            try:
                real = candidate.expanduser().resolve()
            except OSError:
                continue
            if not real.exists():
                continue
            if source.executable and not os.access(real, os.X_OK):
                continue
            if source.requires_any and not any(
                    (real / child).exists() for child in source.requires_any):
                continue
            resolved[source.relative] = real
            break
    return resolved


# -- the lane table -----------------------------------------------------------


@dataclass(frozen=True)
class LaneParity:
    """What one lane needs on disk before its fragment will emit any flag."""

    key: str
    label: str
    flags: tuple[str, ...]
    governance: tuple[str, ...] = ()
    host_sources: tuple[str, ...] = ()
    #: Staged when this machine has them and skipped when it does not. The
    #: Xueqiu hot-rank shim is the case: the lane runs its other two
    #: operations without it, so requiring it would cost a Core the whole lane
    #: over one optional subcommand.
    optional_host_sources: tuple[str, ...] = ()
    #: Any one of these is enough (the HKEX and ownership lanes turn on per
    #: operation, so one approved record already buys a lane).
    governance_any: bool = False
    mission_plan: str | None = None
    mission_sources: tuple[str, ...] = ()
    owner_inputs: tuple[str, ...] = ()
    seeds: tuple[str, ...] = ()
    note: str = ""


#: State files that are neither a contract nor a corpus: an empty, owner-editable
#: default whose *presence* is what a flag needs.  Seeded once and never
#: replaced, exactly as ``deploy/macos/install.sh`` seeds them into the legacy
#: Core -- a workspace created before this table existed has none of them.
SEED_FILES: Mapping[str, Mapping[str, Any]] = {
    "market-proxy-mappings.json": {"schema_version": "0.1", "mappings": []},
}


def _kind_of(filename: str) -> str:
    """The governance kind a record filename names, without its version."""

    stem = filename.removesuffix(".json")
    kind, _, _ = stem.rpartition("-v")
    return kind or stem


def lane_parity_table() -> tuple[LaneParity, ...]:
    """The lanes whose inputs are host-level, packaged or mission-derived.

    Built from each lane module's own constants rather than from a second list
    here: the filename a lane looks for is that lane's contract, and a copy of
    it in this module would be a second opinion that goes stale on its own
    schedule.  The imports are inside the function because importing a lane
    module registers it, and the registry must not load as a side effect of
    reading this table.
    """

    from .hkex_filings_core import DAILY_BUYBACK_TAPE_OPERATION
    from .hkex_filings_launcher import (
        GOVERNANCE_FILENAME_BY_OPERATION as HKEX_GOVERNANCE,
    )
    from .mission_catalyst_lane import CALENDAR_GOVERNANCE
    from .mission_consensus_lane import CONSENSUS_GOVERNANCE
    from .mission_crowd_source_lane import GOVERNANCE_FILES as CROWD_GOVERNANCE
    from .mission_feed_lane import COMPANY_WIKI_GOVERNANCE, SALES_NOTES_GOVERNANCE
    from .mission_guidepoint_lane import GUIDEPOINT_LANE_GOVERNANCE
    from .mission_market_price_lane import MARKET_PRICE_GOVERNANCE
    from .mission_prior_research_lane import GOVERNANCE as PRIOR_RESEARCH_GOVERNANCE
    from .sec_ownership_launcher import (
        GOVERNANCE_FILENAME_BY_OPERATION as OWNERSHIP_GOVERNANCE,
    )

    crowd_records = tuple(
        name for source in sorted(CROWD_GOVERNANCE)
        for name in CROWD_GOVERNANCE[source].values()
    )
    return (
        LaneParity(
            key="market_price", label="行情与价格异动",
            flags=("--market-price-governance", "--market-proxy-config"),
            governance=(MARKET_PRICE_GOVERNANCE,),
            seeds=("market-proxy-mappings.json",),
            note="公开行情，无需账号；代理映射先放一份空的，由你自己填。",
        ),
        LaneParity(
            key="catalyst_calendar", label="事件日历",
            flags=("--catalyst-calendar-governance",),
            governance=(CALENDAR_GOVERNANCE,),
            note="公开日历，无需账号。",
        ),
        LaneParity(
            key="consensus", label="卖方一致预期",
            flags=("--consensus-governance",),
            governance=(CONSENSUS_GOVERNANCE,),
            note="公开一致预期，无需账号。",
        ),
        LaneParity(
            key="sec_ownership", label="美股持股与内部人交易",
            flags=("--sec-ownership-governance-dir",),
            governance=tuple(sorted(OWNERSHIP_GOVERNANCE.values())),
            governance_any=True,
            note="SEC 公开披露，逐项审批：装上哪一项就跑哪一项。",
        ),
        LaneParity(
            key="hkex_filings", label="港股披露",
            flags=("--hkex-filings-governance-dir",),
            # The daily buy-back tape is left out on purpose, exactly as
            # ``deploy/macos/install.sh`` leaves it out: it is a separately
            # governed full-market acquisition, and installing it here would
            # turn a new network permission into setup policy.  The lane turns
            # on per operation, so the other three still buy the lane.
            governance=tuple(sorted(
                name for operation, name in HKEX_GOVERNANCE.items()
                if operation != DAILY_BUYBACK_TAPE_OPERATION)),
            governance_any=True,
            note="港交所公开披露；每日回购带宽另行审批，此处不装。",
        ),
        LaneParity(
            key="guidepoint", label="专家访谈资料（Guidepoint）",
            flags=("--guidepoint-search-governance", "--guidepoint-discovery-plan",
                   "--guidepoint-mcp-endpoint"),
            governance=(GUIDEPOINT_LANE_GOVERNANCE,),
            mission_plan="guidepoint",
            mission_sources=("source:guidepoint",),
            note="MCP 端点是代码常量，不需要安装；缺的是按本任务公司生成的检索计划。",
        ),
        LaneParity(
            key="sales_notes", label="卖方销售快报",
            flags=("--sales-notes-digest-dir", "--sales-notes-governance-list",
                   "--sales-notes-governance-get", "--feed-discovery-plan"),
            governance=tuple(SALES_NOTES_GOVERNANCE),
            host_sources=("feeds/market-digest-output",),
            mission_plan="feed",
            mission_sources=("source:sales-notes",),
        ),
        LaneParity(
            key="company_wiki", label="公司知识库",
            flags=("--company-wiki-corpus-root", "--company-wiki-index-db",
                   "--company-wiki-governance-list", "--company-wiki-governance-get",
                   "--feed-discovery-plan"),
            governance=tuple(COMPANY_WIKI_GOVERNANCE),
            host_sources=("feeds/company-wiki",),
            mission_plan="feed",
            mission_sources=("source:company-wiki",),
        ),
        LaneParity(
            key="prior_research", label="既有研究资料",
            flags=("--prior-research-corpus-root", "--prior-research-governance-list",
                   "--prior-research-governance-get", "--feed-discovery-plan"),
            governance=tuple(PRIOR_RESEARCH_GOVERNANCE),
            host_sources=("feeds/prior-research",),
            mission_plan="feed",
            mission_sources=("source:prior-research",),
        ),
        LaneParity(
            key="crowd_source", label="散户与员工舆情",
            flags=("--crowd-source-map", "--crowd-source-governance-dir"),
            governance=crowd_records,
            host_sources=("host-tools/xueqiu", "host-tools/xreach",
                          "credential-grants/xueqiu.json",
                          "credential-grants/xreach.json"),
            optional_host_sources=("host-tools/xueqiu-hot-rank",),
            mission_plan="crowd",
            mission_sources=("source:xueqiu", "source:x", "source:blind"),
            owner_inputs=("每家公司的 X 账号与员工评价站 slug 无法推导，需要你补。",),
        ),
    )


def host_provisioned_source_refs(host_sources: Mapping[str, Path]) -> list[str]:
    """The source refs a first mission may grant because this machine has them.

    Two conditions, and both are deliberate.  The lane must actually need a
    mission grant -- the yfinance, SEC-ownership and HKEX lanes never ask the
    mission anything, so naming their sources in a source plan would be an
    authority statement that buys nothing.  And its host input must be present
    -- a machine with no crowd tools produces a mission that never claimed a
    crowd source, and the audit says why.  A lane with a grant requirement and
    no host input at all (Guidepoint: an account, not a directory) is left to
    the shared connection catalog, which is where an account belongs.
    """

    refs: set[str] = set()
    for lane in lane_parity_table():
        if not lane.host_sources or not lane.mission_sources:
            continue
        if not all(name in host_sources for name in lane.host_sources):
            continue
        refs.update(lane.mission_sources)
    return sorted(refs)


# -- the mission --------------------------------------------------------------


def read_active_mission(state_dir: str | Path) -> dict[str, Any] | None:
    """The published mission, read without opening the database for writing.

    ``DaltonStore`` would be the obvious way and is the wrong one here: this
    runs against a live environment whose writer holds that file, and an audit
    that creates a WAL beside a running writer is an audit that changed
    something.  A read-only URI connection cannot.
    """

    database = Path(state_dir).expanduser().resolve() / "core.sqlite"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT v.record_json AS record_json FROM coverage_mission_pointer p "
            "JOIN coverage_mission_versions v "
            "ON v.mission_version_id = p.mission_version_id "
            "ORDER BY p.mission_ref LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    try:
        mission = json.loads(row["record_json"])
    except (TypeError, ValueError):
        return None
    return mission if isinstance(mission, Mapping) else None


def connected_source_refs(mission: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(mission, Mapping):
        return set()
    return {
        str(item.get("source_ref"))
        for item in (mission.get("source_plan") or [])
        if isinstance(item, Mapping) and item.get("status") == "connected"
    }


def unknown_universe_tickers(
    mission: Mapping[str, Any] | None,
    feed_plan: Mapping[str, Any] | None = None,
) -> list[str]:
    """Covered tickers nothing on this machine can recognise in a document.

    The three feed lanes attribute a note or a wiki page by looking for the
    company's *names* in its text.  A ticker with no name anywhere -- not in
    the mission, not in this mission's feed plan, not in the packaged fallback
    -- makes the lane refuse every tick, which looks exactly like a broken lane
    and is in fact a missing name.  Better said out loud by the audit than
    discovered in a tick summary.
    """

    from .mission_company_names import mission_name_table, unnamed_tickers

    if not isinstance(mission, Mapping):
        return []
    return unnamed_tickers(
        mission_name_table(list(mission.get("universe") or []), feed_plan))


def _industry_terms(mission: Mapping[str, Any]) -> list[str]:
    """Words that make a document worth reading, taken from the mission itself.

    Only the mission's own words: its industry ref and its title.  Nothing is
    guessed at, because a wrong term here is a lane that reads the wrong
    documents all week.
    """

    raw = str(mission.get("industry_ref") or "").removeprefix("industry:")
    terms: set[str] = set()
    label = raw.replace("-", " ").strip()
    if label:
        terms.add(label)
    title = str(mission.get("title") or "").strip()
    if title:
        terms.add(title[:80])
    # Whole phrases only.  Splitting the industry ref into words looked
    # tempting and produced "ai" and "美国" as keywords -- terms that match
    # nearly every document on the disk, which is the same as having no plan.
    return sorted(terms)


def _universe(mission: Mapping[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in mission.get("universe") or []:
        if not isinstance(item, Mapping):
            continue
        company_ref = str(item.get("company_ref") or "").strip()
        ticker = str(item.get("ticker") or "").strip().upper()
        if company_ref and ticker:
            rows.append((company_ref, ticker))
    return sorted(rows)


def build_mission_feed_plan(
    mission: Mapping[str, Any],
    *,
    company_names: Mapping[str, Sequence[str]] | None = None,
    resolve_name: Any = None,
) -> dict[str, Any]:
    """The feed discovery plan this mission's universe implies.

    The plan carries what each covered issuer is *called*, not only its ticker.
    That is the whole point of generating it: attribution asks "does this note
    name Microsoft", and before this the only answer was a five-row dict in
    ``document_subject`` that no workspace could edit, so every feed lane on a
    non-IT-services mission refused each tick.  A name comes from the mission
    member, else ``company_names``, else the issuer registry through
    ``resolve_name``, else the packaged fallback.

    ``peer_names`` is the one field with no mission answer -- a peer is by
    definition a company the mission does *not* cover -- so it carries the
    covered issuers' own names instead of a guess.  That is deliberately the
    weakest possible claim: it says "a document naming one of my companies is
    worth reading", which is true, and it leaves the owner a file to add real
    peers to rather than a lane that will not start.
    """

    from .mission_company_names import (
        mission_name_table, resolve_universe_names, unnamed_tickers,
    )
    from .mission_feed_lane import validate_feed_discovery_plan
    from .store import content_hash

    universe = _universe(mission)
    if not universe:
        raise LaneParityError("mission universe is empty; no feed plan can be derived")
    industry = _industry_terms(mission)
    if not industry:
        raise LaneParityError("mission names no industry; no feed plan can be derived")
    members = list(mission.get("universe") or [])
    extra = dict(company_names or {})
    extra.update(resolve_universe_names(
        [item for item in members
         if str(item.get("ticker") or "").strip().upper() in set(
             unnamed_tickers(mission_name_table(members, extra=extra)))],
        resolve=resolve_name))
    table = mission_name_table(members, extra=extra)
    missing = unnamed_tickers(table)
    if missing:
        raise LaneParityError(
            "no name is known for " + "、".join(missing)
            + "；资料通道靠公司名字把文档归属到公司，只有 ticker 的话每一轮都会归属不到。"
              "请用 --company-name TICKER=名称 补上，或让 SEC 名称解析可用。")
    body = {
        "schema_version": "0.1",
        "id": f"feed-discovery-plan:{mission['mission_ref'].split(':', 1)[-1]}:1",
        "created_at": "1970-01-01T00:00:00.000000+00:00",
        "mission_ref": mission["mission_ref"],
        "source_refs": ["source:company-wiki", "source:prior-research",
                        "source:sales-notes"],
        "companies": {ref: {"search_terms": ticker,
                            "names": list(table[ticker.upper()])}
                      for ref, ticker in universe},
        "industry_keywords": industry,
        "peer_names": sorted({name for ref, ticker in universe
                              for name in table[ticker.upper()]}),
        "lookback_days": 400,
        "body_reads_per_tick": 50,
    }
    return validate_feed_discovery_plan({**body, "content_hash": content_hash(body)})


def build_mission_guidepoint_plan(mission: Mapping[str, Any]) -> dict[str, Any]:
    """The Guidepoint plan this mission implies, with no borrowed questions.

    The legacy plan's four industry questions are four things a person decided
    about US IT services; copying them into another industry would be the one
    mistake this whole module exists to avoid.  What is derivable is the shape:
    ask each covered company what clients are saying and where it wins, and ask
    the industry the mission's own objective.  The owner edits the file after,
    which is cheaper than an owner who has no lane.
    """

    from .mission_guidepoint_lane import build_guidepoint_discovery_plan

    universe = _universe(mission)
    if not universe:
        raise LaneParityError("mission universe is empty; no Guidepoint plan can be derived")
    industry_label = (str(mission.get("industry_ref") or "")
                      .removeprefix("industry:").replace("-", " ").strip())
    if not industry_label:
        raise LaneParityError("mission names no industry; no Guidepoint plan can be derived")
    budget = mission.get("budget") or {}
    calls = int(budget.get("max_alphaengine_calls_24h") or 20)
    objective = str(mission.get("objective") or mission.get("title") or "").strip()
    objective = objective.replace("{", "").replace("}", "")[:200] or industry_label
    return build_guidepoint_discovery_plan(
        plan_id=f"discovery-plan:{mission['mission_ref'].split(':', 1)[-1]}:guidepoint:1",
        created_at="1970-01-01T00:00:00.000000+00:00",
        mission_ref=mission["mission_ref"],
        companies={ref: ticker for ref, ticker in universe},
        industry_anchor_company_ref=universe[0][0],
        max_calls_24h=max(1, min(calls, 20)),
        specs=[
            {"spec_ref": "client-demand-and-budgets",
             "query_template": "What are clients saying about {terms} demand, budgets and spending plans",
             "document_type": "expert_call_transcript", "lookback_days": 400,
             "max_excerpts": 12, "rediscovery_interval_days": 14,
             "retry_interval_days": 2},
            {"spec_ref": "competitive-wins-and-losses",
             "query_template": "Where does {terms} win or lose competitive deals, and against whom",
             "document_type": "expert_call_transcript", "lookback_days": 540,
             "max_excerpts": 12, "rediscovery_interval_days": 21,
             "retry_interval_days": 2},
        ],
        industry_specs=[
            {"spec_ref": "mission-objective",
             "query": objective, "industry": industry_label[:120],
             "document_type": "expert_call_transcript", "lookback_days": 400,
             "max_excerpts": 12, "rediscovery_interval_days": 14,
             "retry_interval_days": 2},
        ],
    )


def build_mission_crowd_map(mission: Mapping[str, Any]) -> dict[str, Any]:
    """The crowd map this mission's universe implies, with the guesses left out.

    One field is derivable and two are not.  A retail forum is searched by the
    company's name, which the mission has; a corporate X account and an
    employer-review slug are facts about a company that nothing derives, and
    the map's own loader treats an absent field as "this source has nothing for
    this company".  So the Xueqiu channel is mapped and the other two are left
    for the owner -- an honest partial map, rather than handles that would fill
    one company's file with another company's chatter.
    """

    universe = _universe(mission)
    if not universe:
        raise LaneParityError("mission universe is empty; no crowd map can be derived")
    suffix = mission["mission_ref"].split(":", 1)[-1]
    return {
        "schema_version": "0.1",
        "id": f"crowd-source-map:{suffix}:v1",
        "industry_ref": mission.get("industry_ref"),
        "note": ("由任务范围自动生成：只填了可以推导的雪球检索词。"
                 "X 账号与员工评价站 slug 没有任何算法可以推出来，"
                 "留空表示这家公司在这个来源上没有内容；请你补上再让这两条通道跑起来。"),
        "companies": [
            {"company_ref": company_ref, "ticker": ticker, "xueqiu_query": ticker}
            for company_ref, ticker in universe
        ],
    }


# -- planning -----------------------------------------------------------------


@dataclass(frozen=True)
class ParityAction:
    """One thing a repair would add, named so a dry run reads as a list."""

    kind: str
    target: str
    detail: str
    reason: str

    def as_wire(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target,
                "detail": self.detail, "reason": self.reason}


def _governance_status(path: Path) -> str | None:
    try:
        wire = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(wire.get("status")) if isinstance(wire, Mapping) else None


def plan_parity_actions(
    state_dir: str | Path,
    *,
    actor_ref: str,
    host_sources: Mapping[str, Path] | None = None,
    mission: Mapping[str, Any] | None = None,
    lanes: Sequence[str] | None = None,
) -> list[ParityAction]:
    """Everything this state directory is missing that this machine can supply.

    Pure: it reads the state directory and returns a list.  The dry run prints
    exactly this, and ``apply_parity_actions`` performs exactly this, so what
    the owner reads and what happens cannot drift apart.

    ``actor_ref`` is checked here rather than only at apply time because a plan
    is the thing a person reads and agrees to, and a plan that would sign an
    approval as ``automation:something`` is not a plan anyone should be shown.
    """

    if not isinstance(actor_ref, str) or not actor_ref.startswith("human:") \
            or not actor_ref.removeprefix("human:").strip():
        raise LaneParityError(
            "a connector approval is signed by a person; actor_ref must be human:...")
    state = Path(state_dir).expanduser().resolve()
    sources = (resolve_host_sources() if host_sources is None else dict(host_sources))
    selected = None if lanes is None else set(lanes)
    actions: list[ParityAction] = []
    governance_dir = state / "connector-governance"
    planned_records: set[str] = set()
    for lane in lane_parity_table():
        if selected is not None and lane.key not in selected:
            continue
        if lane.host_sources and not all(name in sources for name in lane.host_sources):
            continue
        for name in (*lane.host_sources, *lane.optional_host_sources):
            if name not in sources:
                continue
            target = state / name
            if target.exists() or target.is_symlink():
                continue
            actions.append(ParityAction(
                kind="link", target=str(target), detail=str(sources[name]),
                reason=f"{lane.label}：把本机已有的来源接进工作区（链接而非复制，"
                       "因为宿主还会继续往里写）。",
            ))
        for name in lane.seeds:
            target = state / name
            if target.exists():
                continue
            actions.append(ParityAction(
                kind="seed", target=str(target), detail=name,
                reason=f"{lane.label}：放一份空的默认设置，它在场这条通道才会带上对应参数；"
                       "已有的文件不会被覆盖。",
            ))
        for record in lane.governance:
            if record in planned_records:
                continue
            path = governance_dir / record
            if _governance_status(path) == "approved":
                continue
            planned_records.add(record)
            actions.append(ParityAction(
                kind="governance", target=str(path), detail=_kind_of(record),
                reason=f"{lane.label}：安装本仓库打包的连接契约并按本工作区所有者署名批准。",
            ))
        if lane.mission_plan is not None and mission is not None:
            action = _mission_plan_action(state, lane, mission)
            if action is not None and all(
                    action.target != existing.target for existing in actions):
                actions.append(action)
    return actions


def _mission_plan_action(state: Path, lane: LaneParity,
                         mission: Mapping[str, Any]) -> ParityAction | None:
    targets = {
        "feed": state / "feed-plans" / MISSION_FEED_PLAN_NAME,
        "guidepoint": state / "discovery-plans" / MISSION_GUIDEPOINT_PLAN_NAME,
        "crowd": state / "phase9" / MISSION_CROWD_MAP_NAME,
    }
    target = targets[lane.mission_plan]
    if target.is_file():
        return None
    return ParityAction(
        kind="mission_plan", target=str(target), detail=lane.mission_plan,
        reason=f"{lane.label}：按本任务的公司范围生成检索计划"
               "（研究基线一直把它列为按任务生成，但首次发布并没有真的生成）。",
    )


def apply_parity_actions(
    actions: Sequence[ParityAction], *, actor_ref: str,
    mission: Mapping[str, Any] | None = None,
    company_names: Mapping[str, Sequence[str]] | None = None,
    resolve_name: Any = None,
) -> list[dict[str, str]]:
    """Perform a plan, once, never replacing what the owner already has."""

    from .connector_governance import build_governance_record

    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    performed: list[dict[str, str]] = []
    for action in actions:
        target = Path(action.target)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if action.kind == "link":
            if target.exists() or target.is_symlink():
                performed.append({**action.as_wire(), "result": "preserved"})
                continue
            os.symlink(Path(action.detail), target)
            performed.append({**action.as_wire(), "result": "linked"})
            continue
        if action.kind == "seed":
            _write_json(target, SEED_FILES[action.detail])
            performed.append({**action.as_wire(), "result": "seeded"})
            continue
        if action.kind == "governance":
            record = build_governance_record(
                action.detail, approved_by=actor_ref, status="approved",
                effective_from=now,
            )
            _write_json(target, record)
            performed.append({**action.as_wire(), "result": "approved"})
            continue
        if action.kind == "mission_plan":
            if mission is None:
                raise LaneParityError(
                    "a mission-generated plan needs the published mission")
            try:
                if action.detail == "feed":
                    value = build_mission_feed_plan(
                        mission, company_names=company_names,
                        resolve_name=resolve_name)
                else:
                    value = {"guidepoint": build_mission_guidepoint_plan,
                             "crowd": build_mission_crowd_map}[action.detail](mission)
            except LaneParityError as exc:
                # One plan this mission cannot yield yet -- most often a
                # company nobody has named -- must not undo the other
                # twenty-nine things this repair is doing. Recorded with its
                # reason and repeated by the audit afterwards, which is the
                # difference between "skipped" and "silently skipped".
                performed.append({**action.as_wire(), "result": "skipped",
                                  "detail": f"{action.detail}: {exc}"})
                continue
            _write_json(target, value)
            performed.append({**action.as_wire(), "result": "generated"})
            continue
        raise LaneParityError(f"unknown parity action: {action.kind}")
    return performed


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Owner-only, atomic, and never over an existing file."""

    if path.exists():
        return
    temporary = path.with_name(f".{path.name}.parity.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, (json.dumps(value, ensure_ascii=False, indent=2,
                                         sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


# -- the audit ----------------------------------------------------------------


def writer_lane_flags(state_dir: str | Path) -> set[str]:
    """The flags this state directory would give a freshly rendered writer.

    Derived from ``lane_registry.lane_argv`` -- the same call the LaunchAgent
    render makes -- so the audit cannot disagree with the installer about what
    a lane needs.
    """

    from .lane_registry import LaunchAgentContext, lane_argv

    state = Path(state_dir).expanduser().resolve()
    return {token for token in lane_argv(LaunchAgentContext(state=state))
            if token.startswith("--")}


def installed_writer_flags(plist_path: str | Path) -> set[str] | None:
    """The flags the writer is running with right now, read from its plist."""

    import plistlib

    path = Path(plist_path).expanduser()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
    except (OSError, ValueError):
        return None
    return {str(token) for token in value.get("ProgramArguments") or []
            if str(token).startswith("--")}


def audit_lanes(
    state_dir: str | Path,
    *,
    host_sources: Mapping[str, Path] | None = None,
    plist_path: str | Path | None = None,
) -> dict[str, Any]:
    """Per lane: is it on this writer, and if not, what exactly is missing.

    Read-only by construction.  Nothing here opens a socket, writes a file or
    loads a LaunchAgent; the mission is read through a read-only connection and
    the plist is parsed, not re-rendered.
    """

    state = Path(state_dir).expanduser().resolve()
    sources = (resolve_host_sources() if host_sources is None else dict(host_sources))
    mission = read_active_mission(state)
    granted = connected_source_refs(mission)
    would_render = writer_lane_flags(state)
    installed = (None if plist_path is None
                 else installed_writer_flags(plist_path))
    unknown_tickers = unknown_universe_tickers(mission, _installed_feed_plan(state))
    rows: list[dict[str, Any]] = []
    for lane in lane_parity_table():
        missing_host = [name for name in lane.host_sources if name not in sources]
        records = [(name, _governance_status(state / "connector-governance" / name))
                   for name in lane.governance]
        approved = [name for name, status in records if status == "approved"]
        if lane.governance_any:
            missing_records = [] if approved else [name for name, _ in records]
        else:
            missing_records = [name for name, status in records if status != "approved"]
        blockers: list[str] = []
        for name in missing_host:
            source = next(item for item in HOST_SOURCES if item.relative == name)
            blockers.append(f"本机没有找到{source.label}"
                            + (f"（可用 {source.environment} 指定）"
                               if source.environment else ""))
        missing_records = sorted(missing_records)
        if missing_records:
            blockers.append("连接契约尚未批准：" + "、".join(missing_records))
        plan_missing = _missing_mission_plan(state, lane)
        if plan_missing is not None:
            blockers.append(plan_missing)
        ungranted = [ref for ref in lane.mission_sources if ref not in granted]
        if ungranted and mission is not None:
            blockers.append("研究任务没有把这些来源标为已连接：" + "、".join(ungranted)
                            + "；需要你发布一个新的任务版本才能改。")
        if lane.mission_plan == "feed" and unknown_tickers:
            blockers.append(
                "还不知道这些公司叫什么：" + "、".join(unknown_tickers)
                + "。资料通道靠公司名字把文档归属到公司，只有 ticker 的话它会启动但"
                  "每一轮都归属不到；把名称写进本任务的资料通道检索计划"
                  f"（{MISSION_FEED_PLAN_NAME} 的 companies[].names），"
                  "或用修复脚本的 --company-name TICKER=名称。")
        configured = all(flag in would_render for flag in lane.flags)
        rows.append({
            "key": lane.key, "label": lane.label,
            "configured": configured,
            "missing_flags": sorted(flag for flag in lane.flags
                                    if flag not in would_render),
            "missing_host_sources": missing_host,
            "missing_governance": missing_records,
            "approved_governance": approved,
            "blockers": blockers,
            "owner_inputs": list(lane.owner_inputs),
            "note": lane.note,
        })
    stale = None
    if installed is not None:
        stale = sorted(would_render - installed)
    return {
        "schema_version": SCHEMA_VERSION,
        "state_dir": str(state),
        "mission_ref": (mission or {}).get("mission_ref"),
        "granted_source_refs": sorted(granted),
        "host_sources": {name: str(path) for name, path in sorted(sources.items())},
        "lanes": rows,
        "configured_lanes": sorted(row["key"] for row in rows if row["configured"]),
        "unconfigured_lanes": sorted(row["key"] for row in rows if not row["configured"]),
        "plist_missing_flags": stale,
    }


def _installed_feed_plan(state: Path) -> dict[str, Any] | None:
    """The feed plan this state directory would run on, if it has one."""

    from .mission_feed_lane import resolve_feed_plan
    from .mission_prior_research_lane import FEED_PLAN_NAME

    path = resolve_feed_plan(state, FEED_PLAN_NAME)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _missing_mission_plan(state: Path, lane: LaneParity) -> str | None:
    if lane.mission_plan is None:
        return None
    from .mission_crowd_source_lane import CROWD_SOURCE_MAP
    from .mission_guidepoint_lane import GUIDEPOINT_LANE_PLAN
    from .mission_prior_research_lane import FEED_PLAN_NAME

    candidates = {
        "feed": ((state / "feed-plans" / MISSION_FEED_PLAN_NAME,
                  state / "feed-plans" / FEED_PLAN_NAME), "资料通道检索计划"),
        "guidepoint": ((state / "discovery-plans" / MISSION_GUIDEPOINT_PLAN_NAME,
                        state / "discovery-plans" / GUIDEPOINT_LANE_PLAN),
                       "Guidepoint 检索计划"),
        "crowd": ((state / "phase9" / MISSION_CROWD_MAP_NAME,
                   state / "phase9" / CROWD_SOURCE_MAP), "舆情来源映射"),
    }
    paths, label = candidates[lane.mission_plan]
    if any(path.is_file() for path in paths):
        return None
    return f"{label}尚未按本任务生成（{paths[0].name}）"


def render_audit(report: Mapping[str, Any]) -> str:
    """The owner-facing page.  One block per lane, and never a bare word."""

    lines = [f"工作区状态目录：{report['state_dir']}",
             f"研究任务：{report.get('mission_ref') or '尚未发布'}", ""]
    for row in report["lanes"]:
        mark = "已装" if row["configured"] else "未装"
        lines.append(f"[{mark}] {row['label']}（{row['key']}）")
        if row["note"]:
            lines.append(f"    说明：{row['note']}")
        for blocker in row["blockers"]:
            lines.append(f"    缺：{blocker}")
        for item in row["owner_inputs"]:
            lines.append(f"    需你决定：{item}")
        if row["configured"] and not row["blockers"]:
            lines.append("    这条通道的输入齐了。")
        lines.append("")
    if report.get("plist_missing_flags"):
        lines.append("已安装的 writer 启动项落后于状态目录，重新渲染后会多出这些参数：")
        lines.extend(f"    {flag}" for flag in report["plist_missing_flags"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "HOST_SOURCES", "MISSION_CROWD_MAP_NAME", "MISSION_FEED_PLAN_NAME",
    "MISSION_GUIDEPOINT_PLAN_NAME", "SCHEMA_VERSION", "HostSource",
    "LaneParity", "LaneParityError", "ParityAction", "apply_parity_actions",
    "audit_lanes", "build_mission_crowd_map", "build_mission_feed_plan",
    "build_mission_guidepoint_plan", "connected_source_refs",
    "host_provisioned_source_refs", "installed_writer_flags",
    "lane_parity_table", "plan_parity_actions", "read_active_mission",
    "render_audit", "resolve_host_sources", "unknown_universe_tickers",
    "writer_lane_flags",
]
