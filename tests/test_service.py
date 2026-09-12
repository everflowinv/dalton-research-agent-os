from __future__ import annotations

import contextlib
import concurrent.futures
import io
import json
import plistlib
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.dashboard import ProjectionWriter
from dalton_core.bootstrap import bootstrap
from dalton_core.plugins.static_dashboard import (
    StaticDashboardError,
    TencentCosConfig,
    render_static_dashboard,
)
from dalton_core.observability import ObservabilityStore
from dalton_core.macos_launchagent import (
    CONTROL_LABEL,
    CONTROLLER_LABEL,
    THESIS_IMPACT_LABEL,
    WRITER_LABEL,
    render,
)
from dalton_core.health import check
from dalton_core.agenda_coordinator import AgendaCoordinator
from dalton_core.scheduler import Scheduler
from dalton_core.service import DaltonService, ServiceConfig, ServiceConfigError
from dalton_core.store import DaltonStore
from dalton_core.writer_server import (
    DASHBOARD_CONTROL_OPERATIONS,
    RESEARCH_REVIEW_CONTROL_OPERATIONS,
    WriterServerError,
    load_principals,
)
from tests.test_dashboard import snapshot


class StaticDashboardTests(unittest.TestCase):
    def test_render_embeds_projection_and_escapes_script_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projection = root / "projection.sqlite"
            data = snapshot()
            data["workflow_summaries"][0]["title"] = "</script><b>unsafe</b>"
            with ProjectionWriter(projection) as writer:
                writer.replace(data)
            output = root / "public" / "index.html"
            result = render_static_dashboard(projection, output)
            html = output.read_text(encoding="utf-8")
            self.assertNotIn("const EMBEDDED_DATA = null", html)
            self.assertNotIn("</script><b>unsafe", html)
            self.assertIn("\\u003c/script\\u003e", html)
            self.assertEqual(result["projection_watermark"], "watermark:42")

    def test_cos_publisher_is_scoped_to_dalton_key(self) -> None:
        raw = {
            "bucket": "bucket",
            "region": "region",
            "key": "index.html",
            "public_url": "https://example.com/dalton/",
            "keychain_account": "account",
            "secret_id_service": "id",
            "secret_key_service": "key",
            "protected_urls": [],
        }
        with self.assertRaises(StaticDashboardError):
            TencentCosConfig.from_mapping(raw)


class InstallerSeedTests(unittest.TestCase):
    """INT1: what the installer puts in place for the merged Wave 1 lanes.

    Read out of the script rather than run, like every other assertion about
    it: running it installs a virtualenv and LaunchAgents. What is worth
    pinning is that the two yfinance records are seeded the way every other
    connector record is -- copied once, never overwritten, owner-only, and
    left *proposed* so the approval stays the owner's -- and that the library
    the lane refuses without is actually installed.
    """

    INSTALL = Path(__file__).resolve().parents[1] / "deploy" / "macos" / "install.sh"

    def script(self) -> str:
        return self.INSTALL.read_text(encoding="utf-8")

    def test_both_yfinance_records_are_seeded_once_and_owner_only(self) -> None:
        text = self.script()
        for kind in ("yfinance-daily-prices", "yfinance-analyst-estimates"):
            self.assertIn(kind, text)
            record = (
                self.INSTALL.parents[1] / "connector-governance" / f"{kind}-v1.json"
            )
            self.assertTrue(record.is_file(), f"{kind} record is not in the repo")
            # Seeded as proposed: the installer never approves anything.
            self.assertEqual(
                json.loads(record.read_text(encoding="utf-8"))["status"], "proposed")
        block = text.split("for yfinance_kind in", 1)[1].split("done", 1)[0]
        self.assertIn('if [[ ! -f "$yfinance_file"', block)
        self.assertIn('chmod 600 "$yfinance_file"', block)

    def test_the_market_data_extra_is_installed(self) -> None:
        # Without yfinance the price lane refuses with a reason rather than
        # guessing, which is correct and also means the lane never runs.
        self.assertIn(
            '"${repo_root}[deploy,pdf,sec-financials,market-data,prior-models,hk-filings]"', self.script())

    # INT2: what each block below puts on disk, and what that switches on.
    # The value is the files the installer copies (relative to the state
    # directory) and the writer argument the lane's own fragment emits once
    # they are all there. A lane whose entry is ``None`` is one this script
    # deliberately does not install; the reason is in the comment beside it.
    LANE_SEEDS = {
        "catalyst": (
            ["connector-governance/yfinance-calendar-v1.json"],
            "--catalyst-calendar-governance",
        ),
        "guidepoint": (
            ["connector-governance/guidepoint-search-library-v1.json",
             "discovery-plans/us-it-services-guidepoint-v1.json"],
            "--guidepoint-discovery-plan",
        ),
        "sales_notes": (
            ["feed-plans/p9-us-it-services-feeds-v2.json",
             "connector-governance/sales-notes-list-notes-v1.json",
             "connector-governance/sales-notes-get-note-v1.json"],
            "--sales-notes-digest-dir",
        ),
        "company_wiki": (
            ["feed-plans/p9-us-it-services-feeds-v2.json",
             "connector-governance/company-wiki-list-documents-v1.json",
             "connector-governance/company-wiki-get-document-v1.json"],
            "--company-wiki-corpus-root",
        ),
        # W3: the fund's own earlier work, behind the directory the owner
        # declares. Same three files as the two S1 feeds; the corpus link is
        # made in the same block and is what the fragment checks for.
        "prior_research": (
            ["feed-plans/p9-us-it-services-feeds-v2.json",
             "connector-governance/prior-research-list-documents-v1.json",
             "connector-governance/prior-research-get-document-v1.json"],
            "--prior-research-corpus-root",
        ),
        "tracking": (["tracking-policy.json"], "--tracking-policy"),
    }

    def lane_argv(self, state: Path) -> list[str]:
        from dalton_core.lane_registry import LaunchAgentContext, lane_argv

        return lane_argv(LaunchAgentContext(state=state))

    def test_the_calendar_and_tracking_lanes_are_one_file_each(self) -> None:
        text = self.script()
        self.assertIn("yfinance-calendar-v1.json", text)
        self.assertIn("p14a-tracking-policy-v2.json", text)
        record = (self.INSTALL.parents[1] / "connector-governance"
                  / "yfinance-calendar-v1.json")
        # Seeded as proposed: the installer never approves anything.
        self.assertEqual(
            json.loads(record.read_text(encoding="utf-8"))["status"], "proposed")

    def test_each_seeded_lane_is_switched_on_by_exactly_what_is_seeded(self) -> None:
        # The all-or-nothing rule, checked against the lanes rather than
        # against the script's prose: put down what the block puts down and
        # the lane's own fragment has to turn it on. Half of it and the lane
        # must stay absent, because a lane that starts and refuses every tick
        # reads like a fault rather than an absence.
        repo = self.INSTALL.parents[2]
        sources = {
            "connector-governance/yfinance-calendar-v1.json":
                repo / "deploy/connector-governance/yfinance-calendar-v1.json",
            "connector-governance/guidepoint-search-library-v1.json":
                repo / "deploy/connector-governance/guidepoint-search-library-v1.json",
            "discovery-plans/us-it-services-guidepoint-v1.json":
                repo / "deploy/phase9/p9-us-it-services-guidepoint-v1.json",
            "feed-plans/p9-us-it-services-feeds-v2.json":
                repo / "deploy/phase9/p9-us-it-services-feeds-v2.json",
            "connector-governance/sales-notes-list-notes-v1.json":
                repo / "deploy/connector-governance/sales-notes-list-notes-v1.json",
            "connector-governance/sales-notes-get-note-v1.json":
                repo / "deploy/connector-governance/sales-notes-get-note-v1.json",
            "connector-governance/company-wiki-list-documents-v1.json":
                repo / "deploy/connector-governance/company-wiki-list-documents-v1.json",
            "connector-governance/company-wiki-get-document-v1.json":
                repo / "deploy/connector-governance/company-wiki-get-document-v1.json",
            "connector-governance/prior-research-list-documents-v1.json":
                repo / "deploy/connector-governance/prior-research-list-documents-v1.json",
            "connector-governance/prior-research-get-document-v1.json":
                repo / "deploy/connector-governance/prior-research-get-document-v1.json",
            "tracking-policy.json":
                repo / "deploy/phase9/p14a-tracking-policy-v2.json",
        }
        for lane, (needs, flag) in self.LANE_SEEDS.items():
            with tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                for name in needs:
                    target = state / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(sources[name].read_bytes())
                if lane == "sales_notes":
                    (state / "feeds" / "market-digest-output").mkdir(parents=True)
                if lane == "company_wiki":
                    corpus = state / "feeds" / "company-wiki"
                    corpus.mkdir(parents=True)
                    (corpus / "wiki-index.sqlite").write_bytes(b"")
                if lane == "prior_research":
                    (state / "feeds" / "prior-research").mkdir(parents=True)
                self.assertIn(flag, self.lane_argv(state), f"{lane} did not switch on")
                # And one file short is the whole lane absent.
                (state / needs[-1]).unlink()
                self.assertNotIn(flag, self.lane_argv(state),
                                 f"{lane} switched on with a file missing")

    def test_the_crowd_lane_needs_the_owner_to_approve_before_it_runs(self) -> None:
        # The seven records ship proposed and the lane reads the *status*, not
        # the file's presence, so seeding them switches nothing on. That is
        # deliberate and this pins it: the installer's job is to put the
        # decision in front of the owner, not to make it.
        repo = self.INSTALL.parents[2]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            governance = state / "connector-governance"
            governance.mkdir(parents=True)
            (state / "phase9").mkdir()
            (state / "phase9" / "p9-us-it-services-crowd-sources-v1.json").write_bytes(
                (repo / "deploy/phase9/p9-us-it-services-crowd-sources-v1.json").read_bytes())
            for name in ("xueqiu-search-posts", "xueqiu-get-post", "xueqiu-hot-rank",
                         "x-xreach-user-timeline", "x-xreach-search", "x-xreach-thread",
                         "employee-reviews-blind"):
                source = repo / f"deploy/connector-governance/{name}-v1.json"
                self.assertTrue(source.is_file(), f"{name} record is not in the repo")
                record = json.loads(source.read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "proposed")
                (governance / f"{name}-v1.json").write_text(
                    json.dumps(record), encoding="utf-8")
            self.assertNotIn("--crowd-source-map", self.lane_argv(state))
            approved = json.loads(
                (governance / "xueqiu-search-posts-v1.json").read_text(encoding="utf-8"))
            approved["status"] = "approved"
            (governance / "xueqiu-search-posts-v1.json").write_text(
                json.dumps(approved), encoding="utf-8")
            self.assertIn("--crowd-source-map", self.lane_argv(state))

    def test_the_lanes_this_script_will_not_install(self) -> None:
        # Deliberate absences: the installer cannot supply the other half.
        code = "\n".join(line for line in self.script().splitlines()
                          if not line.lstrip().startswith("#"))
        # P14e's lane switch, withheld until a template has been published.
        self.assertNotIn("research-task-lane.json", code)
        # S4 has no lane in this wave at all, so its six records switch
        # nothing on and cannot half-switch-on anything.
        self.assertIn("cn-hk-findata", code)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            governance = state / "connector-governance"
            governance.mkdir(parents=True)
            repo = self.INSTALL.parents[2]
            for kind in ("financial-statements", "shareholders", "buybacks",
                         "margin-balance", "northbound-flow", "ah-premium"):
                source = repo / f"deploy/connector-governance/cn-hk-findata-{kind}-v1.json"
                self.assertTrue(source.is_file())
                self.assertEqual(
                    json.loads(source.read_text(encoding="utf-8"))["status"], "proposed")
                (governance / source.name).write_bytes(source.read_bytes())
            self.assertNotIn("cn-hk", " ".join(self.lane_argv(state)))

    def test_the_feed_and_crowd_lanes_are_off_without_their_environment(self) -> None:
        # A Core installed without OpenClaw has no feeds and no host tools, and
        # must end up with no feed lane and no crowd lane rather than with
        # three that refuse every tick. The gate is an environment variable
        # with a documented default, not an assumption about the host.
        text = self.script()
        self.assertIn("DALTON_OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace", text)
        for name in ("DALTON_AGENT_REACH_TOOL", "DALTON_XUEQIU_HOT_RANK_TOOL",
                     "DALTON_XREACH_TOOL"):
            self.assertIn(name, text)
        # Each guarded block prints why it did nothing rather than being silent.
        self.assertIn("the sales-note lane is not installed", text)
        self.assertIn("the company-wiki lane is not installed", text)
        self.assertIn("to install the crowd-source lane", text)

    def test_the_narrowing_record_is_not_put_where_a_lane_would_load_it(self) -> None:
        # It is the note the owner reads before deciding what to do with an
        # approval for an operation the upstream does not have. Nothing loads
        # it, and a permanently-proposed record sitting in the runtime
        # directory is an approval to make about nothing.
        text = self.script()
        block = text.split("decisions_dir=", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn("governance-decisions", block)
        self.assertIn("guidepoint-get-transcript-narrowing-v1.json", block)
        self.assertNotIn("$governance_dir", block)
        # And it never lands in the directory the cockpit and the lanes read.
        self.assertNotIn(
            '$governance_dir/guidepoint-get-transcript-narrowing-v1.json', text)

    def test_the_probe_template_manifest_is_publication_material(self) -> None:
        text = self.script()
        self.assertIn("p14e-adhoc-probe-templates-v1.json", text)
        manifest = (self.INSTALL.parents[2]
                    / "deploy/phase8/p14e-adhoc-probe-templates-v1.json")
        self.assertTrue(manifest.is_file())
        # The installer signs nothing: every template is published by the
        # owner under a human: principal.
        self.assertNotIn("publish_probe_template", text)

    def test_the_cockpit_is_told_where_the_gateway_catalog_is(self) -> None:
        # The control process must not go looking through the host's home
        # directory on its own, so the path is written into the config it
        # already reads -- and only when the gateway is actually installed.
        text = self.script()
        head, body = text.split("<<'PYBROKER'", 1)
        guard = head.rsplit("if [[ -f", 1)[1]
        self.assertIn(".openclaw/openclaw.json", guard)
        block = body.split("PYBROKER", 1)[0]
        self.assertIn("openclaw_config_path", block)
        self.assertIn('"cockpit"', block)

    def test_gateway_catalog_binding_is_byte_idempotent_when_already_exact(self) -> None:
        import subprocess
        import sys

        block = self.script().split("<<'PYBROKER'", 1)[1].split("PYBROKER", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = root / "openclaw.json"
            broker.write_text("{}\n", encoding="utf-8")
            config = root / "service.json"
            value = {"owner": {"signature": "preserve"}, "control": {"config": {
                "cockpit": {"openclaw_config_path": str(broker)}}}}
            config.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            before = config.read_bytes()
            for _ in range(2):
                result = subprocess.run(
                    [sys.executable, "-", str(config), str(broker)], input=block,
                    capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(before, config.read_bytes())

    def test_the_script_parses(self) -> None:
        import subprocess

        for shell in ("bash", "zsh"):
            result = subprocess.run(
                [shell, "-n", str(self.INSTALL)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class DeliberatelyUnseededTests(unittest.TestCase):
    """INT3: no committed governance record can be forgotten.

    Nineteen committed records were seeded by nothing, four of them on purpose
    and the script said so in a comment. A comment is not a check: the other
    fifteen -- ``yfinance-calendar-v1.json`` among them, one block away from
    turning the catalyst lane on -- read exactly the same from outside.

    So the rule is now checkable. Every record in ``deploy/connector-governance``
    is either copied into the runtime governance directory by its lane's block,
    or named in the ``DELIBERATELY_UNSEEDED`` array with the reason beside it.
    A record that is in neither fails here rather than becoming a lane that
    reports ``unconfigured`` for ever with nobody having decided that.
    """

    INSTALL = Path(__file__).resolve().parents[1] / "deploy" / "macos" / "install.sh"
    REPO = Path(__file__).resolve().parents[1]

    def sets(self) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
        from scripts.rehearse_deploy import (
            committed_governance_records,
            deliberately_unseeded_records,
            install_seeded_records,
        )

        return (
            committed_governance_records(self.REPO),
            install_seeded_records(self.INSTALL),
            deliberately_unseeded_records(self.INSTALL),
        )

    def test_committed_equals_seeded_plus_deliberately_unseeded(self) -> None:
        committed, seeded, named = self.sets()
        self.assertEqual(
            committed - seeded - named, frozenset(),
            "seed these in their lane's all-or-nothing block, or name them in "
            "DELIBERATELY_UNSEEDED in install.sh with the reason",
        )
        self.assertEqual(
            named - committed, frozenset(),
            "DELIBERATELY_UNSEEDED names a record the repo does not carry",
        )
        self.assertEqual(seeded & named, frozenset())

    def test_every_deliberate_absence_carries_a_reason(self) -> None:
        # The array is read by nothing at runtime; its whole value is that the
        # reason sits next to the decision. An entry with no comment above it
        # is the comment-only state this replaced.
        text = self.INSTALL.read_text(encoding="utf-8")
        body = text.split("DELIBERATELY_UNSEEDED=(", 1)[1].split("\n)", 1)[0]
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        previous = None
        for line in lines:
            kind = "comment" if line.startswith("#") else "name"
            if kind == "name" and previous != "name":
                self.assertEqual(previous, "comment",
                                 f"{line} starts a group with no reason above it")
            previous = kind
        self.assertEqual(previous, "name", "the array ends on a dangling comment")

    def test_the_reasons_are_the_ones_that_were_decided(self) -> None:
        _, _, named = self.sets()
        self.assertEqual(
            named,
            frozenset({
                "hkex-filings-daily-buyback-tape-v1.json",
                "roic-list-transcripts-v1.json",
                "roic-get-transcript-v1.json",
                "guidepoint-get-transcript-narrowing-v1.json",
            }),
        )

    def test_the_calendar_record_is_seeded_now(self) -> None:
        # The one that mattered: C1's lane was a single block away and the
        # record had been committed for a day.
        _, seeded, _ = self.sets()
        self.assertIn("yfinance-calendar-v1.json", seeded)


class FilingsIndexRecoveryTests(unittest.TestCase):
    """INT3: ``sec-filings-index-v1.json`` existed on one machine.

    The writer's plist names it unconditionally, so a Core rebuilt from scratch
    got ``--sec-filings-governance`` pointing at a path with no file behind it.
    The record is recovered from the live Core, and what makes that safe to
    commit is that it is not a copy of somebody's disk: it re-derives from the
    packaged SEC template, hash for hash.
    """

    REPO = Path(__file__).resolve().parents[1]
    RECORD = REPO / "deploy" / "connector-governance" / "sec-filings-index-v1.json"

    def record(self) -> dict:
        return json.loads(self.RECORD.read_text(encoding="utf-8"))

    def test_the_record_is_committed(self) -> None:
        self.assertTrue(self.RECORD.is_file())

    def test_it_re_derives_from_the_packaged_contract(self) -> None:
        from dalton_core.sec_filings_index import (
            build_filings_index_governance_record,
        )
        from dalton_core.store import canonical_json

        live = self.record()
        built = build_filings_index_governance_record(
            approved_by=live["approved_by"],
            status=live["status"],
            effective_from=live["effective_from"],
            max_lease_seconds=live["max_lease_seconds"],
        )
        self.assertEqual(canonical_json(built), canonical_json(live))
        self.assertEqual(built["content_hash"], live["content_hash"])

    def test_its_own_content_hash_verifies(self) -> None:
        from dalton_core.store import content_hash

        live = self.record()
        base = {key: value for key, value in live.items() if key != "content_hash"}
        self.assertEqual(content_hash(base), live["content_hash"])

    def test_the_approved_status_is_preserved_and_has_precedent(self) -> None:
        # Seeding is copy-once, so re-proposing this here would take the
        # owner's own approval away on the next rebuild rather than ask for
        # one. Two committed records already carry an approval for the same
        # reason -- they were approved before the repo carried them.
        self.assertEqual(self.record()["status"], "approved")
        self.assertTrue(self.record()["approved_by"].startswith("human:"))
        for precedent in ("alphaengine-get-document-v1.json",
                          "sec-company-facts-v1.json"):
            other = json.loads(
                (self.RECORD.parent / precedent).read_text(encoding="utf-8"))
            self.assertEqual(other["status"], "approved")

    def test_the_bytes_are_the_canonical_form(self) -> None:
        from dalton_core.store import canonical_json

        raw = self.RECORD.read_bytes()
        self.assertEqual(
            raw, (canonical_json(json.loads(raw)) + "\n").encode("utf-8"))

    def test_install_seeds_it_with_the_lane_plan(self) -> None:
        from scripts.rehearse_deploy import install_seeded_records

        install = self.REPO / "deploy" / "macos" / "install.sh"
        self.assertIn("sec-filings-index-v1.json", install_seeded_records(install))
        # All or nothing: the record and the lane's plan are one block.
        text = install.read_text(encoding="utf-8")
        block = text.split("sec_filings_governance_file=", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn("p10-us-it-services-sec-filings-plan-v1.json", block)

    def test_the_writer_plist_argument_now_has_a_file_behind_it(self) -> None:
        from scripts.rehearse_deploy import INSTALL_SEEDS

        self.assertIn(
            "connector-governance/sec-filings-index-v1.json",
            [spec.state for spec in INSTALL_SEEDS],
        )


class BootstrapSchemaTests(unittest.TestCase):
    """INT3: every packaged schema is applied by ``dalton-bootstrap``.

    It used to open five authorities. The other forty-seven schemas ran the
    first time the writer or a lane constructed their authority, which on a
    live Core is several minutes after ``install.sh`` has already exited zero:
    a deploy that broke a schema did not fail the install, it failed one lane
    on one tick, in a heartbeat nobody was reading.
    """

    def test_every_packaged_schema_names_a_database(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES, packaged_schema_files

        self.assertEqual(
            frozenset(packaged_schema_files()) - frozenset(dict(SCHEMA_DATABASES)),
            frozenset(),
            "a new *_schema.sql needs an entry, or the install would exit zero "
            "without ever applying it",
        )
        self.assertEqual(
            frozenset(dict(SCHEMA_DATABASES)) - frozenset(packaged_schema_files()),
            frozenset(),
        )

    def test_the_pairs_that_share_a_file_stay_in_order(self) -> None:
        from dalton_core.bootstrap import SCHEMA_DATABASES

        order = [name for name, _ in SCHEMA_DATABASES]
        self.assertLess(order.index("candidate_staging_schema.sql"),
                        order.index("research_review_schema.sql"))
        self.assertLess(order.index("thesis_impact_budget_schema.sql"),
                        order.index("budget_pools_schema.sql"))

    def test_bootstrap_applies_them_all_and_repeats_cleanly(self) -> None:
        from dalton_core.bootstrap import bootstrap, packaged_schema_files

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = bootstrap(root / "state", root / "service.json")
            self.assertEqual(
                int(first["schemas_applied"]), len(packaged_schema_files()))
            second = bootstrap(root / "state", root / "service.json")
            self.assertEqual(first, second)

    def test_it_creates_no_database_the_deploy_would_not(self) -> None:
        # A sidecar the Core does not have is applied into a scratch database
        # and thrown away: the SQL still runs, and an operator is not left with
        # an empty document-index.sqlite to explain.
        from dalton_core.bootstrap import bootstrap

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = bootstrap(root / "state", root / "service.json")
            self.assertGreater(int(result["schemas_applied_to_scratch"]), 0)
            names = {path.name for path in (root / "state").glob("*.sqlite")}
            self.assertEqual(
                names, {"core.sqlite", "scheduler.sqlite", "model-router.sqlite"})
            self.assertFalse((root / "state" / "research-review").exists())

    def test_a_broken_schema_fails_here(self) -> None:
        # The whole point: this is a sqlite3 error raised out of
        # dalton-bootstrap, so install.sh exits non-zero under `set -e`.
        # Before, it was one lane on one tick, minutes after the install had
        # already reported success.
        from dalton_core.bootstrap import apply_packaged_schemas

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            package = Path(directory) / "package"
            root.mkdir()
            package.mkdir()
            (package / "good_schema.sql").write_text(
                "CREATE TABLE IF NOT EXISTS good(id TEXT PRIMARY KEY);",
                encoding="utf-8")
            (package / "broken_schema.sql").write_text(
                "CREATE TABLE IF NOT EXISTS broken(", encoding="utf-8")
            with self.assertRaises(sqlite3.Error):
                apply_packaged_schemas(
                    root, package=package,
                    schemas=(("good_schema.sql", None),
                             ("broken_schema.sql", None)))

    def test_a_schema_with_no_declared_database_is_refused(self) -> None:
        # A new *_schema.sql nobody added to the table would otherwise be
        # skipped in silence, which is the state this whole change is about.
        from dalton_core.bootstrap import apply_packaged_schemas

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            package = Path(directory) / "package"
            root.mkdir()
            package.mkdir()
            (package / "brand_new_schema.sql").write_text(
                "CREATE TABLE IF NOT EXISTS x(id TEXT);", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                apply_packaged_schemas(root, package=package, schemas=())
        self.assertIn("brand_new_schema.sql", str(caught.exception))


class LegacyAgendaPlaneRetirementTests(unittest.TestCase):
    """ADR-0009: the daily agenda/perception run is off unless asked for by name."""

    def raw(self, root: Path) -> dict:
        return {
            "schema_version": "0.1",
            "core_db": str(root / "core.sqlite"),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "projection_db": str(root / "projection.sqlite"),
            "model_router_db": None,
            "capability_catalog_db": None,
            "heartbeat_path": str(root / "run" / "heartbeat.json"),
            "writer_socket": str(root / "run" / "writer.sock"),
            "tick_seconds": 1,
            "projection_min_interval_seconds": 1,
            "plugin_retry_seconds": 1,
            "plugins": [],
            "agenda": {
                "enabled": True,
                "interval_seconds": 86400,
                "config": {
                    "scheduler_db": str(root / "scheduler.sqlite"),
                    "model_router_db": str(root / "router.sqlite"),
                    "writer_socket": str(root / "run" / "writer.sock"),
                    "core_token_config": str(root / "tokens.json"),
                    "broker_socket": str(root / "run" / "broker.sock"),
                    "broker_auth_key": str(root / "broker.key"),
                    "perception_source_db": str(root / "legacy-coverage.sqlite"),
                    "perception_snapshot_path": str(root / "perception.json"),
                    "company_ref": "wanhua",
                    "routing_policy_ref": "routing-policy:agenda",
                    "credential_slot_refs": ["slot:openclaw"],
                    "broker_client_id": "client:dalton-core",
                    "expected_agent_id": "chem",
                    "timeout_seconds": 180.0,
                },
            },
        }

    def test_an_enabled_agenda_block_alone_no_longer_builds_the_coordinator(self) -> None:
        # The retirement has to survive the config that is already on the live
        # machine. ``enabled: true`` was the whole switch before ADR-0009, so
        # if it still worked the plane would come back on the next restart
        # without anyone deciding that.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with DaltonStore(root / "core.sqlite") as store:
                ObservabilityStore(store)
            config = ServiceConfig.from_mapping(self.raw(root))
            self.assertFalse(config.legacy_agenda_plane)
            self.assertIsNone(config.agenda)
            self.assertIsNone(config.agenda_interval_seconds)
            service = DaltonService(config)
            try:
                self.assertIsNone(service._agenda)
                self.assertEqual("retired", service._agenda_state["state"])
                with contextlib.redirect_stderr(io.StringIO()) as captured:
                    service.start()
                    service.start()
                self.assertIsNone(service._agenda_executor)
                self.assertEqual(
                    ["legacy agenda plane retired (ADR-0009)"],
                    captured.getvalue().splitlines(),
                )
                heartbeat = service.run_once(force_projection=True)
            finally:
                service.close()
            self.assertEqual("retired", heartbeat["agenda"]["state"])
            self.assertIsNone(heartbeat["agenda"]["last_started_at"])
            self.assertEqual("running", heartbeat["state"])

    def test_the_named_opt_in_restores_the_plane_unchanged(self) -> None:
        # Reversible by one key: with the opt-in the coordinator is built from
        # the same block, on the same interval, with its own single-thread
        # executor -- and no retirement line is logged.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self.raw(root)
            raw["legacy_agenda_plane"] = True
            config = ServiceConfig.from_mapping(raw)
            self.assertTrue(config.legacy_agenda_plane)
            self.assertEqual(86400.0, config.agenda_interval_seconds)
            self.assertEqual("wanhua", config.agenda.company_ref)
            self.assertEqual(
                root / "legacy-coverage.sqlite", config.agenda.perception_source_db
            )
            service = DaltonService(config)
            try:
                self.assertIsInstance(service._agenda, AgendaCoordinator)
                self.assertEqual("pending", service._agenda_state["state"])
                with contextlib.redirect_stderr(io.StringIO()) as captured:
                    service.start()
                self.assertEqual("", captured.getvalue())
                self.assertIsNotNone(service._agenda_executor)
            finally:
                service.close()

    def test_the_opt_in_is_boolean_and_the_block_shape_is_still_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = self.raw(root)
            raw["legacy_agenda_plane"] = "true"
            with self.assertRaises(ServiceConfigError):
                ServiceConfig.from_mapping(raw)
            # Retired does not mean unread: a malformed block is still a
            # config error rather than something the retirement swallows.
            broken = self.raw(root)
            broken["agenda"].pop("interval_seconds")
            with self.assertRaises(ServiceConfigError):
                ServiceConfig.from_mapping(broken)
            # And an unknown top-level key is still refused, so the opt-in
            # cannot be smuggled in under a near-miss spelling.
            typo = self.raw(root)
            typo["legacy_agenda_planes"] = True
            with self.assertRaises(ServiceConfigError):
                ServiceConfig.from_mapping(typo)

    def test_a_fresh_install_never_writes_the_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = bootstrap(root / "state", root / "config" / "service.json")
            written = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
            self.assertNotIn("legacy_agenda_plane", written)
            self.assertNotIn("agenda", written)
            self.assertFalse(ServiceConfig.from_mapping(written).legacy_agenda_plane)


class ServiceTests(unittest.TestCase):
    def test_outbox_waits_for_writer_bearing_planner_without_hiding_prior_result(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = mock.Mock()
        service._outbox.run_once.return_value = {"status": "ready"}
        service._outbox_executor = mock.Mock()
        service._outbox_future = None
        service._outbox_last_launch_monotonic = 0.0
        service._outbox_state = {
            "state": "pending", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }
        service.config = mock.Mock(outbox_interval_seconds=60)
        service._bounded_planner_future = mock.Mock()

        service._poll_outbox()
        service._outbox_executor.submit.assert_not_called()
        self.assertEqual("pending", service._outbox_state["state"])

        # Completion is still observed while the planner is busy; only a new
        # writer RPC is deferred.
        completed = mock.Mock()
        completed.done.return_value = True
        completed.result.return_value = {"status": "ready", "claimed": 0}
        service._outbox_future = completed
        service._poll_outbox()
        self.assertEqual("ready", service._outbox_state["state"])
        self.assertIsNone(service._outbox_state["last_error"])
        service._outbox_executor.submit.assert_not_called()

        service._bounded_planner_future = None
        service._poll_outbox()
        service._outbox_executor.submit.assert_called_once_with(service._outbox.run_once)
        self.assertEqual("running", service._outbox_state["state"])

    def test_planner_and_outbox_take_the_single_writer_in_due_order(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = mock.Mock()
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=60, bounded_planner_interval_seconds=60,
        )
        service._outbox_last_launch_monotonic = 0.0
        service._bounded_planner_last_launch_monotonic = 0.0
        service._outbox_future = None
        service._bounded_planner_future = None
        service._outbox_state = {
            "state": "pending", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }
        service._bounded_planner_state = {
            "state": "pending", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }
        outbox_release = threading.Event()
        planner_release = threading.Event()
        service._outbox.run_once.side_effect = lambda: (
            outbox_release.wait(5), {"status": "ready"}
        )[1]
        service._bounded_planner.run_once.side_effect = lambda: (
            planner_release.wait(5), {"status": "completed"}
        )[1]
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as outbox_executor, \
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as planner_executor:
            service._outbox_executor = outbox_executor
            service._bounded_planner_executor = planner_executor
            # Both are initially due. The earlier poll cannot let the planner
            # immediately occupy the writer and starve the outbox.
            service._poll_bounded_planner()
            self.assertIsNone(service._bounded_planner_future)
            service._poll_outbox()
            self.assertIsNotNone(service._outbox_future)
            service._poll_bounded_planner()
            self.assertIsNone(service._bounded_planner_future)

            outbox_release.set()
            service._outbox_future.result(timeout=1)
            service._poll_outbox()
            # Make only the planner due now; it starts after the outbox result
            # has been harvested and no writer-bearing outbox remains.
            service._outbox_last_launch_monotonic = time.monotonic()
            service._poll_bounded_planner()
            self.assertIsNotNone(service._bounded_planner_future)
            service._poll_outbox()
            self.assertIsNone(service._outbox_future)
            planner_release.set()
            service._bounded_planner_future.result(timeout=1)

    def test_due_outbox_gets_turn_after_long_planner_and_planner_error_is_preserved(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = mock.Mock()
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=60, bounded_planner_interval_seconds=60,
        )
        service._outbox_last_launch_monotonic = 0.0
        service._bounded_planner_last_launch_monotonic = 0.0
        service._outbox_future = None
        failed = concurrent.futures.Future()
        failed.set_exception(RuntimeError("planner failed"))
        service._bounded_planner_future = failed
        service._outbox_executor = mock.Mock()
        service._bounded_planner_executor = mock.Mock()
        service._outbox_state = {
            "state": "pending", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }
        service._bounded_planner_state = {
            "state": "running", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }

        service._poll_bounded_planner()
        self.assertIn("planner failed", service._bounded_planner_state["last_error"])
        service._bounded_planner_executor.submit.assert_not_called()
        service._poll_outbox()
        service._outbox_executor.submit.assert_called_once_with(service._outbox.run_once)

    def test_due_planner_gets_turn_after_long_failed_outbox_and_error_is_harvested(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = mock.Mock()
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=60, bounded_planner_interval_seconds=60,
        )
        # The outbox launched after the planner and ran longer than its own
        # interval. Both are due, but planner has waited longer.
        service._bounded_planner_last_launch_monotonic = 1.0
        service._outbox_last_launch_monotonic = 2.0
        failed = concurrent.futures.Future()
        failed.set_exception(RuntimeError("outbox failed"))
        service._outbox_future = failed
        service._bounded_planner_future = None
        service._outbox_executor = mock.Mock()
        service._bounded_planner_executor = mock.Mock()
        service._outbox_state = {
            "state": "running", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }
        service._bounded_planner_state = {
            "state": "pending", "last_started_at": None,
            "last_completed_at": None, "last_error": None, "last_result": None,
        }

        service._poll_bounded_planner()
        service._bounded_planner_executor.submit.assert_called_once_with(
            service._bounded_planner.run_once
        )
        self.assertEqual("running", service._bounded_planner_state["state"])
        # run_once calls this next in the same frame: it harvests the completed
        # outbox error but cannot immediately re-launch over the new planner.
        service._poll_outbox()
        self.assertIn("outbox failed", service._outbox_state["last_error"])
        service._outbox_executor.submit.assert_not_called()

    def test_light_settlement_uses_planner_executor_without_delaying_full_tick(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = mock.Mock()
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=60, bounded_planner_interval_seconds=300,
        )
        now = time.monotonic()
        service._outbox_last_launch_monotonic = now
        service._bounded_planner_last_launch_monotonic = now
        service._child_settlement_last_launch_monotonic = 0.0
        service._outbox_future = None
        service._bounded_planner_future = None
        service._outbox_executor = mock.Mock()
        service._bounded_planner_executor = mock.Mock()
        service._outbox_state = {"state": "ready"}
        service._bounded_planner_state = {"state": "completed"}
        service._child_settlement_state = {"state": "pending"}

        service._poll_bounded_planner()
        service._bounded_planner_executor.submit.assert_called_once_with(
            service._bounded_planner.settle_children_once)
        self.assertEqual(service._bounded_planner_future_kind, "settlement")
        self.assertEqual(service._bounded_planner_last_launch_monotonic, now)

        service._bounded_planner_executor.reset_mock()
        service._bounded_planner_future = None
        service._bounded_planner_last_launch_monotonic = 0.0
        service._outbox_last_launch_monotonic = time.monotonic()
        service._poll_bounded_planner()
        service._bounded_planner_executor.submit.assert_called_once_with(
            service._bounded_planner.run_once)
        self.assertEqual(service._bounded_planner_future_kind, "full")

    def test_light_settlement_result_does_not_replace_full_tick_result(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = None
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=None, bounded_planner_interval_seconds=300,
        )
        service._outbox_last_launch_monotonic = 0.0
        service._bounded_planner_last_launch_monotonic = time.monotonic()
        service._child_settlement_last_launch_monotonic = time.monotonic()
        service._outbox_future = None
        settled = concurrent.futures.Future()
        settled.set_result({"status": "settled", "settled": {"ticket_ref": "ticket:1"}})
        service._bounded_planner_future = settled
        service._bounded_planner_future_kind = "settlement"
        service._outbox_executor = None
        service._bounded_planner_executor = mock.Mock()
        service._bounded_planner_state = {
            "state": "completed", "last_result": {"tick": "full"},
        }
        service._child_settlement_state = {"state": "running"}

        service._poll_bounded_planner()

        self.assertEqual(service._bounded_planner_state["last_result"], {"tick": "full"})
        self.assertEqual(
            service._child_settlement_state["last_result"],
            {"status": "settled", "settled": {"ticket_ref": "ticket:1"}},
        )
        service._bounded_planner_executor.submit.assert_not_called()

    def test_disabled_full_planner_interval_also_disables_settlement(self) -> None:
        service = DaltonService.__new__(DaltonService)
        service._outbox = None
        service._bounded_planner = mock.Mock()
        service.config = mock.Mock(
            outbox_interval_seconds=None, bounded_planner_interval_seconds=None,
        )
        service._bounded_planner_future = None
        service._bounded_planner_executor = mock.Mock()
        service._bounded_planner_last_launch_monotonic = 0.0
        service._child_settlement_last_launch_monotonic = 0.0
        service._outbox_future = None

        service._poll_bounded_planner()

        service._bounded_planner_executor.submit.assert_not_called()

    @staticmethod
    def _maintenance_config(root: Path) -> ServiceConfig:
        core = root / "core.sqlite"
        with DaltonStore(core) as store:
            ObservabilityStore(store)
        return ServiceConfig.from_mapping({
            "schema_version": "0.1", "core_db": str(core),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "projection_db": str(root / "projection.sqlite"),
            "model_router_db": None, "capability_catalog_db": None,
            "heartbeat_path": str(root / "heartbeat.json"),
            "writer_socket": str(root / "writer.sock"), "tick_seconds": 1,
            "projection_min_interval_seconds": 1, "plugin_retry_seconds": 1,
            "plugins": [],
        })

    def test_blocked_lease_sweep_does_not_block_persistent_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = DaltonService(self._maintenance_config(root))
            entered, release = threading.Event(), threading.Event()

            def blocked_sweep():
                entered.set()
                self.assertTrue(release.wait(3))
                return []

            try:
                with mock.patch.object(service, "_perform_sweep", blocked_sweep):
                    first = service.run_once()
                    self.assertTrue(entered.wait(1))
                    before = time.monotonic()
                    second = service.run_once()
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertNotEqual(first["last_tick_at"], second["last_tick_at"])
                    self.assertIsNotNone(service._sweep_future)
                    release.set()
                    service._sweep_future.result(timeout=2)
                    service._poll_sweep(allow_launch=False)
                    self.assertIsNotNone(service._last_sweep_at)
            finally:
                release.set()
                service.close()

    def test_once_drains_lease_sweep_and_exposes_its_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = DaltonService(self._maintenance_config(root))
            entered, release = threading.Event(), threading.Event()

            def failed_sweep():
                entered.set()
                self.assertTrue(release.wait(3))
                raise RuntimeError("sweep read failed")

            def delayed_release():
                self.assertTrue(entered.wait(1))
                time.sleep(0.1)
                release.set()

            releaser = threading.Thread(target=delayed_release)
            releaser.start()
            try:
                with mock.patch.object(service, "_perform_sweep", failed_sweep):
                    before = time.monotonic()
                    heartbeat = service.run_once(
                        force_projection=True, wait_for_projection=True
                    )
                releaser.join(1)
                self.assertGreaterEqual(time.monotonic() - before, 0.09)
                self.assertEqual("degraded", heartbeat["state"])
                self.assertEqual("RuntimeError: sweep read failed", heartbeat["last_error"])
                self.assertIsNone(service._sweep_future)
                self.assertIsNotNone(heartbeat["last_sweep_at"])
            finally:
                release.set()
                service.close()

    def test_successful_lease_sweep_clears_prior_degraded_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DaltonService(self._maintenance_config(Path(directory)))
            calls = 0

            def sweep():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("one failed read")
                return []

            try:
                with mock.patch.object(service, "_perform_sweep", sweep):
                    failed = service.run_once(
                        force_projection=True, wait_for_projection=True
                    )
                    recovered = service.run_once(
                        force_projection=True, wait_for_projection=True
                    )
                self.assertEqual("degraded", failed["state"])
                self.assertEqual("RuntimeError: one failed read", failed["last_error"])
                self.assertEqual("running", recovered["state"])
                self.assertIsNone(recovered["last_error"])
                self.assertEqual(2, calls)
            finally:
                service.close()

    def test_sweep_close_failure_does_not_skip_other_executor_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = DaltonService(self._maintenance_config(Path(directory)))
            service.start()
            scheduler = service._scheduler
            self.assertIsNotNone(scheduler)

            def failed_close():
                assert scheduler is not None
                scheduler.close()
                service._scheduler = None
                raise RuntimeError("close failed after release")

            with mock.patch.object(service, "_close_sweep_scheduler", failed_close):
                with self.assertRaisesRegex(RuntimeError, "close failed after release"):
                    service.close()
            self.assertIsNone(service._sweep_executor)
            self.assertIsNone(service._projection_executor)
            self.assertIsNone(service._plugin_executor)

    def test_scheduler_schema_indexes_only_leased_sweep_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scheduler.sqlite"
            scheduler = Scheduler(path)
            try:
                plan = scheduler.connection.execute(
                    "EXPLAIN QUERY PLAN SELECT e.* FROM scheduler_attempt_events e "
                    "WHERE e.state='leased' AND NOT EXISTS ("
                    " SELECT 1 FROM scheduler_attempt_events newer "
                    " WHERE newer.work_order_id=e.work_order_id "
                    " AND newer.event_seq>e.event_seq) ORDER BY e.event_seq"
                ).fetchall()
            finally:
                scheduler.close()
            detail = "\n".join(str(row[3]) for row in plan)
            self.assertIn("scheduler_attempt_leased_seq", detail)
            self.assertNotIn("SCAN e\n", detail)

    def test_once_harvests_one_real_expiry_without_repeating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._maintenance_config(root)
            scheduler = Scheduler(config.scheduler_db)
            work = {
                "schema_version": "0.1", "id": "work:expiry-service-test",
                "created_at": "2026-09-11T00:00:00+00:00",
                "updated_at": "2026-09-11T00:00:00+00:00",
                "question": "expire this test lease",
                "requested_capabilities": ["test"],
                "runtime_profile_ref": "runtime:test",
                "budget": {"max_seconds": 60},
                "idempotency_key": "enqueue:expiry-service-test",
                "declared_side_effects": [], "status": "ready",
                "input_refs": ["fixture:expiry"], "metadata": {},
            }
            scheduler.enqueue(work)
            self.assertIsNotNone(scheduler.claim(
                "worker:test", work_order_id=work["id"], lease_seconds=0.001
            ))
            scheduler.close()
            time.sleep(0.01)

            service = DaltonService(config)
            try:
                first = service.run_once(
                    force_projection=True, wait_for_projection=True
                )
                second = service.run_once(
                    force_projection=True, wait_for_projection=True
                )
            finally:
                service.close()
            self.assertEqual(1, first["expired_lease_count"])
            self.assertEqual(1, second["expired_lease_count"])

            scheduler = Scheduler(config.scheduler_db)
            try:
                states = [
                    row["state"] for row in scheduler.attempt_history(work["id"])
                ]
            finally:
                scheduler.close()
            self.assertEqual(["ready", "leased", "expired", "ready"], states)

    def test_backup_retention_is_explicit_and_runs_only_after_a_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": "0.1", "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"), "tick_seconds": 1,
                "projection_min_interval_seconds": 1, "plugin_retry_seconds": 1,
                "plugins": [], "backup": {"enabled": True,
                    "root": str(root / "backups"), "interval_seconds": 86400},
            }
            self.assertIsNone(ServiceConfig.from_mapping(raw).backup_keep_latest)
            for invalid in (0, True, -1):
                with self.subTest(invalid=invalid), self.assertRaises(ServiceConfigError):
                    ServiceConfig.from_mapping({
                        **raw, "backup": {**raw["backup"], "keep_latest": invalid}})
            raw["backup"]["keep_latest"] = 3
            service = DaltonService(ServiceConfig.from_mapping(raw))
            retention = {"status": "pruned", "keep_latest": 3,
                         "deleted_count": 2, "deleted_bytes": 100}
            with mock.patch.object(service._backup, "snapshot",
                                   return_value={"snapshot_id": "snapshot-new"}) as snapshot, \
                    mock.patch.object(service._backup, "prune_verified",
                                      return_value=retention) as prune:
                service.start()
                service._run_backup()
                service._backup_future.result(timeout=2)
                service._poll_backup(allow_launch=False)
            snapshot.assert_called_once_with()
            prune.assert_called_once_with(keep_latest=3)
            self.assertEqual(service._backup_state["last_retention"], retention)
            self.assertEqual(service._backup_state["state"], "ready")
            service.close()

    def test_persistent_tick_keeps_heartbeating_with_one_backup_in_flight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1", "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"), "tick_seconds": 1,
                "projection_min_interval_seconds": 1, "plugin_retry_seconds": 1,
                "plugins": [], "backup": {"enabled": True,
                    "root": str(root / "backups"), "interval_seconds": 86400},
            }
            service = DaltonService(ServiceConfig.from_mapping(raw))
            started, release = threading.Event(), threading.Event()
            calls = []

            def blocked_snapshot():
                calls.append("snapshot")
                started.set()
                self.assertTrue(release.wait(3))
                return {"snapshot_id": "snapshot-new"}

            with mock.patch.object(service._backup, "snapshot",
                                   side_effect=blocked_snapshot):
                try:
                    before = time.monotonic()
                    first = service.run_once()
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertTrue(started.wait(1))
                    self.assertEqual(first["backup"]["state"], "running")
                    second = service.run_once()
                    self.assertEqual(second["backup"]["state"], "running")
                    self.assertEqual(calls, ["snapshot"])

                    def delayed_release():
                        time.sleep(0.1)
                        release.set()

                    releaser = threading.Thread(target=delayed_release)
                    releaser.start()
                    before_close = time.monotonic()
                    service.close()
                    releaser.join(1)
                    self.assertGreaterEqual(time.monotonic() - before_close, 0.09)
                finally:
                    release.set()
                    service.close()
            self.assertEqual(service._backup_state["state"], "ready")
            self.assertEqual(service._backup_state["last_snapshot_id"],
                             "snapshot-new")

    def test_once_waits_for_backup_and_reports_background_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1", "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"), "tick_seconds": 1,
                "projection_min_interval_seconds": 1, "plugin_retry_seconds": 1,
                "plugins": [], "backup": {"enabled": True,
                    "root": str(root / "backups"), "interval_seconds": 86400},
            }
            service = DaltonService(ServiceConfig.from_mapping(raw))
            started, release = threading.Event(), threading.Event()

            def failed_snapshot():
                started.set()
                self.assertTrue(release.wait(3))
                raise RuntimeError("integrity failed")

            def delayed_release():
                self.assertTrue(started.wait(1))
                time.sleep(0.1)
                release.set()

            releaser = threading.Thread(target=delayed_release)
            releaser.start()
            with mock.patch.object(
                service._backup, "snapshot", side_effect=failed_snapshot
            ) as snapshot:
                before = time.monotonic()
                result = service.run_once(
                    force_projection=True, wait_for_projection=True)
                releaser.join(1)
                self.assertGreaterEqual(time.monotonic() - before, 0.09)
                self.assertEqual(result["state"], "degraded")
                self.assertEqual(result["backup"]["state"], "error")
                self.assertIn("integrity failed", result["backup"]["last_error"])
                self.assertIsNone(service._backup_future)

                # A failed multi-database integrity pass is still an attempt.
                # Do not spend another one on every five-second controller tick.
                service._poll_backup()
                self.assertIsNone(service._backup_future)
                self.assertEqual(snapshot.call_count, 1)
                with mock.patch(
                    "dalton_core.service.time.monotonic",
                    return_value=(service._last_backup_monotonic + 86401),
                ):
                    service._poll_backup()
                retry = service._backup_future
                self.assertIsNotNone(retry)
                retry.exception(timeout=2)
                service._poll_backup(allow_launch=False)
                self.assertEqual(snapshot.call_count, 2)
            service.close()

    def test_one_cycle_sweeps_projects_and_renders_without_an_llm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1",
                "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [
                    {
                        "type": "static_dashboard",
                        "enabled": True,
                        "output_path": str(root / "public" / "index.html"),
                        "publisher": None,
                    }
                ],
            }
            service = DaltonService(ServiceConfig.from_mapping(raw))
            try:
                # ``daltond --once`` uses this wait mode and must retain its
                # original complete projection + rendered artifact contract.
                heartbeat = service.run_once(
                    force_projection=True, wait_for_projection=True
                )
            finally:
                service.close()
            self.assertEqual(heartbeat["state"], "running")
            self.assertEqual("disabled", heartbeat["weekly_brief"]["state"])
            self.assertEqual("disabled", heartbeat["bounded_planner"]["state"])
            self.assertIsNotNone(heartbeat["projection_watermark"])
            self.assertEqual(heartbeat["plugins"]["static_dashboard"]["state"], "ready")
            self.assertTrue((root / "projection.sqlite").is_file())
            self.assertTrue((root / "public" / "index.html").is_file())
            saved = json.loads((root / "run" / "heartbeat.json").read_text())
            self.assertEqual(saved["service"], "daltond")

    def test_blocked_dashboard_publish_does_not_block_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1", "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1, "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [{"type": "static_dashboard", "enabled": True,
                             "output_path": str(root / "public" / "index.html"),
                             "publisher": None}],
            }
            entered = threading.Event()
            release = threading.Event()

            def blocked(_plugin, _projection):
                entered.set()
                release.wait(5)
                return {"render": {"sha256": "a" * 64}, "publish": None}

            service = DaltonService(ServiceConfig.from_mapping(raw))
            try:
                with mock.patch(
                    "dalton_core.plugins.static_dashboard.StaticDashboardPlugin.on_projection",
                    blocked,
                ):
                    first = service.run_once(force_projection=True)
                    for _ in range(100):
                        first = service.run_once()
                        if entered.wait(0.01):
                            break
                    self.assertTrue(entered.wait(1))
                    before = time.monotonic()
                    second = service.run_once(force_projection=True)
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertNotEqual(first["last_tick_at"], second["last_tick_at"])
                    self.assertEqual(second["plugins"]["static_dashboard"]["state"], "running")
                    self.assertEqual(len(service._plugin_futures), 1)
                    release.set()
                    for _ in range(100):
                        heartbeat = service.run_once()
                        if heartbeat["plugins"]["static_dashboard"]["state"] == "ready":
                            break
                        time.sleep(0.01)
                    self.assertEqual(heartbeat["plugins"]["static_dashboard"]["state"], "ready")
            finally:
                release.set()
                service.close()

    def test_blocked_projection_does_not_block_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1", "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1, "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1, "plugins": [],
            }
            entered = threading.Event()
            release = threading.Event()

            calls = []

            def blocked(*_args, **_kwargs):
                calls.append(True)
                entered.set()
                release.wait(5)
                return {"metadata": {"source_watermark": "sha256:" + "a" * 64}}

            service = DaltonService(ServiceConfig.from_mapping(raw))
            try:
                with mock.patch("dalton_core.service.project_dashboard", blocked):
                    first = service.run_once(force_projection=True)
                    self.assertTrue(entered.wait(1))
                    before = time.monotonic()
                    second = service.run_once(force_projection=True)
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertNotEqual(first["last_tick_at"], second["last_tick_at"])
                    release.set()
                    for _ in range(100):
                        heartbeat = service.run_once()
                        if heartbeat["projection_watermark"] is not None:
                            break
                        time.sleep(0.01)
                    self.assertEqual(
                        heartbeat["projection_watermark"], "sha256:" + "a" * 64
                    )
                    # A source change observed during the old snapshot is not
                    # swallowed by completing that snapshot.
                    with mock.patch.object(service, "_sources", return_value=("new",)):
                        service._last_projection_monotonic = 0.0
                        service.run_once()
                        for _ in range(100):
                            if len(calls) == 2:
                                break
                            time.sleep(0.01)
                        self.assertEqual(len(calls), 2)
            finally:
                release.set()
                service.close()

    def test_projection_blocks_retry_of_old_failed_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core = root / "core.sqlite"
            with DaltonStore(core) as store:
                ObservabilityStore(store)
            raw = {
                "schema_version": "0.1", "core_db": str(core),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1, "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [{"type": "static_dashboard", "enabled": True,
                             "output_path": str(root / "public" / "index.html"),
                             "publisher": None}],
            }
            entered, release = threading.Event(), threading.Event()

            def blocked(*_args, **_kwargs):
                entered.set(); release.wait(5)
                return {"metadata": {"source_watermark": "sha256:" + "b" * 64}}

            service = DaltonService(ServiceConfig.from_mapping(raw))
            try:
                state = service._plugin_states["static_dashboard"]
                state.update(state="error", retry_at_monotonic=0.0)
                with mock.patch("dalton_core.service.project_dashboard", blocked), mock.patch.object(
                    service, "_run_plugin", wraps=service._run_plugin
                ) as run_plugin:
                    heartbeat = service.run_once(force_projection=True)
                    self.assertTrue(entered.wait(1))
                    service.run_once()
                    run_plugin.assert_not_called()
                    self.assertEqual(heartbeat["projection"]["state"], "running")
            finally:
                release.set(); service.close()

    def test_failed_refresh_keeps_ready_until_failure_is_observed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": "0.1", "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None, "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1, "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 60,
                "plugins": [{"type": "static_dashboard", "enabled": True,
                             "output_path": str(root / "public" / "index.html"),
                             "publisher": None}],
            }
            service = DaltonService(ServiceConfig.from_mapping(raw)); service.start()
            entered, release = threading.Event(), threading.Event()
            service._plugin_states["static_dashboard"].update(
                state="ready", result={"old": True}, last_success_at="old"
            )

            def fail(_plugin, _projection):
                entered.set(); release.wait(5); raise RuntimeError("publish failed")

            try:
                with mock.patch(
                    "dalton_core.plugins.static_dashboard.StaticDashboardPlugin.on_projection", fail
                ):
                    service._run_plugin(service.config.plugins[0])
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(service._plugin_states["static_dashboard"]["state"], "ready")
                    release.set()
                    for _ in range(100):
                        service._poll_plugins()
                        if service._plugin_states["static_dashboard"]["state"] == "error": break
                        time.sleep(0.01)
                    state = service._plugin_states["static_dashboard"]
                    self.assertEqual(state["state"], "error")
                    self.assertEqual(state["result"], {"old": True})
                    self.assertIn("publish failed", state["last_error"])
            finally:
                release.set(); service.close()

    def test_bounded_planner_block_parses_and_reports_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "run" / "heartbeat.json"),
                "writer_socket": str(root / "run" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "bounded_planner": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "writer_socket": str(root / "run" / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "scheduler_db": str(root / "scheduler.sqlite"),
                        "user_agent": "Dalton Research Bounded Planner",
                        "max_response_bytes": 8_388_608,
                        "timeout_seconds": 60.0,
                        "max_probes_per_tick": 1,
                        "filed_window_days": 400,
                        "observation_mandate_version_ref": (
                            "mandate-version:us-it-services-sec-lane:v3"
                        ),
                        "doctrine_pack_version_ref": None,
                        "doctrine_pack_version_hash": None,
                        "planner_routing_policy_ref": None,
                        "planner_credential_slot_refs": None,
                        "planner_model_router_db": None,
                        "planner_broker_socket": None,
                        "planner_broker_auth_key": None,
                        "planner_broker_client_id": "client:dalton-core",
                        "planner_expected_agent_id": "chem",
                        "planner_max_cost_usd": 0.5,
                    },
                },
            }
            config = ServiceConfig.from_mapping(raw)
            self.assertEqual(300.0, config.bounded_planner_interval_seconds)
            self.assertEqual(1, config.bounded_planner.max_probes_per_tick)
            bad = json.loads(json.dumps(raw))
            bad["bounded_planner"]["config"]["surprise"] = True
            with self.assertRaises(ServiceConfigError):
                ServiceConfig.from_mapping(bad)

    def test_config_rejects_arbitrary_plugin_imports(self) -> None:
        raw = {
            "schema_version": "0.1",
            "core_db": "/tmp/core.sqlite",
            "scheduler_db": "/tmp/scheduler.sqlite",
            "projection_db": "/tmp/projection.sqlite",
            "model_router_db": None,
            "capability_catalog_db": None,
            "heartbeat_path": "/tmp/heartbeat.json",
            "writer_socket": "/tmp/writer.sock",
            "tick_seconds": 1,
            "projection_min_interval_seconds": 1,
            "plugin_retry_seconds": 1,
            "plugins": [{"type": "os.system", "enabled": True}],
        }
        with self.assertRaises(ServiceConfigError):
            ServiceConfig.from_mapping(raw)

    def test_weekly_brief_schedule_is_managed_by_the_existing_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "weekly_brief": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "plan": {
                            "schema_version": "0.1",
                            "plan_ref": "weekly-brief-plan:test:v1",
                            "brief_ref": "weekly-brief:test",
                            "timezone": "America/New_York",
                            "weekday": 3, "hour": 7, "minute": 0,
                            "effective_from": "2026-08-27T00:00:00+00:00",
                            "evidence_pack_version_id": "evidence-pack-version:test",
                            "company_overlay_version_ids": ["overlay-version:test"],
                            "company_thesis_refs": {},
                            "destination_ref": "openclaw:discord:test",
                        },
                    },
                },
                "outbox": {
                    "enabled": True,
                    "interval_seconds": 60,
                    "config": {
                        "openclaw_executable": "/usr/bin/true",
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "account": "default", "target": "channel:123",
                        "guild_id": "456", "channel_id": "123",
                        "endpoint_ref": "openclaw:discord:test",
                        "control_url": "https://dalton.example.test/",
                        "company_labels": {}, "feedback_user_ids": [],
                        "timeout_seconds": 30, "claim_ttl_seconds": 120,
                        "retry_seconds": 60, "max_attempts": 5,
                        "batch_size": 1, "feedback_limit": 10,
                        "weekly_brief_attachment_dir": str(root / "attachments"),
                    },
                },
            }
            config = ServiceConfig.from_mapping(raw)
            self.assertEqual(300, config.weekly_brief_interval_seconds)
            self.assertEqual(
                "weekly-brief-plan:test:v1", config.weekly_brief.plan.plan_ref
            )

    def test_launchagents_keep_only_deterministic_services_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = render(
                root / "LaunchAgents",
                root / "venv" / "bin",
                root / "state",
                root / "config.json",
                root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            controller = plistlib.loads(Path(paths["controller"]).read_bytes())
            self.assertEqual(writer["Label"], WRITER_LABEL)
            self.assertEqual(controller["Label"], CONTROLLER_LABEL)
            self.assertTrue(writer["KeepAlive"])
            self.assertTrue(controller["KeepAlive"])
            self.assertIn("dalton-writer", writer["ProgramArguments"][0])
            self.assertIn("--scheduler", writer["ProgramArguments"])
            writer_args = writer["ProgramArguments"]
            self.assertEqual(
                writer_args[writer_args.index("--alphaengine-search-governance") + 1],
                str((root / "state").resolve() / "connector-governance" / "alphaengine-search-library-v1.json"),
            )
            self.assertEqual(
                writer_args[writer_args.index("--alphaengine-discovery-plan") + 1],
                str((root / "state").resolve() / "discovery-plans" / "us-it-services-alphaengine-v1.json"),
            )
            self.assertIn("daltond", controller["ProgramArguments"][0])
            self.assertNotIn("model", " ".join(controller["ProgramArguments"]).lower())

    def test_launchagent_persists_the_explicit_web_search_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "service.json"
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "state" / "core.sqlite"),
                "scheduler_db": str(root / "state" / "scheduler.sqlite"),
                "projection_db": str(root / "state" / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "state" / "heartbeat.json"),
                "writer_socket": str(root / "state" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "web_search_expected_provider": "antigravity",
            }
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            paths = render(
                root / "LaunchAgents", root / "venv" / "bin",
                root / "state", config_path, root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            argv = writer["ProgramArguments"]
            self.assertEqual(
                argv[argv.index("--web-search-expected-provider") + 1],
                "antigravity",
            )

    def test_writer_launchagent_is_standard_process_type_others_background(self) -> None:
        # S7d: writer-hosted children inherit the writer's launchd process
        # type; Background runs CPU work ~6x slower and cannot be lifted from
        # inside the child (measured 2026-08-26).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = render(
                root / "LaunchAgents",
                root / "venv" / "bin",
                root / "state",
                root / "config.json",
                root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            controller = plistlib.loads(Path(paths["controller"]).read_bytes())
            self.assertEqual(writer["ProcessType"], "Standard")
            self.assertEqual(controller["ProcessType"], "Background")

    def test_enabled_thesis_impact_gets_short_lived_launchagent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            raw = {
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": str(root / "router.sqlite"),
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "thesis_impact": {
                    "enabled": True,
                    "interval_seconds": 300,
                    "config": {
                        "scheduler_db": str(root / "scheduler.sqlite"),
                        "model_router_db": str(root / "router.sqlite"),
                        "writer_socket": str(root / "writer.sock"),
                        "token_config": str(root / "tokens.json"),
                        "broker_socket": str(root / "broker.sock"),
                        "broker_auth_key": str(root / "broker.key"),
                        "budget_db": str(root / "budget.sqlite"),
                        "routing_policy_ref": "policy:shared",
                        "assessment_routing_policy_ref": "policy:assessment",
                        "verifier_routing_policy_ref": "policy:verifier",
                        "budget_policy_version_id": "budget:1",
                        "day_cap_micros": 25_000_000,
                        "credential_slot_refs": ["credential-slot:openai", "credential-slot:google"],
                        "broker_client_id": "client:dalton-core",
                        "expected_agent_id": "chem",
                        "company_thesis_refs": {},
                        "max_targets": 25,
                        "timeout_seconds": 180,
                    },
                },
            }
            config.write_text(json.dumps(raw))
            paths = render(
                root / "LaunchAgents",
                root / "venv" / "bin",
                root / "state",
                config,
                root / "logs",
            )
            agent = plistlib.loads(Path(paths["thesis_impact"]).read_bytes())
            self.assertEqual(agent["Label"], THESIS_IMPACT_LABEL)
            self.assertNotIn("KeepAlive", agent)
            self.assertTrue(agent["RunAtLoad"])
            self.assertEqual(agent["StartInterval"], 300)
            self.assertIn("dalton-thesis-impact", agent["ProgramArguments"][0])

    def test_enabled_control_plane_gets_a_separate_launchagent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "control": {"enabled": True, "config": {
                    "host": "127.0.0.1", "port": 8793,
                    "tailscale_host": "dalton.example.ts.net",
                    "tailscale_executable": "/usr/bin/true",
                    "allowed_tailscale_logins": ["owner@example.com"],
                    "writer_socket": str(root / "writer.sock"),
                    "token_config": str(root / "tokens.json"),
                    "endpoint_ref": "openclaw:discord:test",
                    "feedback_timeout_seconds": 86400,
                    "sweep_interval_seconds": 60,
                    "research_review": {
                        "candidate_staging_path": str(
                            root / "candidate-staging.sqlite"
                        ),
                        "transcript_review_directory": str(
                            root / "review-inbox"
                        ),
                        "reconcile_interval_seconds": 60,
                    },
                }},
            }), encoding="utf-8")
            paths = render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )
            control = plistlib.loads(Path(paths["control"]).read_bytes())
            self.assertEqual(control["Label"], CONTROL_LABEL)
            self.assertTrue(control["KeepAlive"])
            self.assertIn("dalton-control", control["ProgramArguments"][0])
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            self.assertIn("--transcript-spool-dir", writer["ProgramArguments"])
            service = ServiceConfig.from_file(config)
            self.assertIsNotNone(service.control.research_review)
            self.assertNotIn("research_review", paths)
            # S7c-3: the writer stages transcript candidates into the very
            # file the Cockpit reviews, derived from the control config.
            writer_args = writer["ProgramArguments"]
            self.assertIn("--candidate-staging", writer_args)
            # S7d: the SEC lane rides on the same staging file and is only
            # wired when that file is configured.
            self.assertIn("--sec-lane-governance", writer_args)
            # P13ad: the deliverable's own drafting model is passed only when
            # the owner installed one. Absent, the Initial Screen keeps being
            # drafted with the extraction configuration, as it always was.
            self.assertNotIn("--initial-screen-model-config", writer_args)
            # P13ak: the statements lane turns on when its approved record is
            # on disk and stays off otherwise -- a Core without one runs
            # exactly as it did.
            self.assertNotIn("--statement-lane-governance", writer_args)
            governance = root / "state" / "connector-governance"
            governance.mkdir(parents=True, exist_ok=True)
            from dalton_core.sec_financials_core import build_sec_financials_governance_record
            (governance / "sec-financial-statements-v2.json").write_text(json.dumps(
                build_sec_financials_governance_record(
                    approved_by="human:test", status="approved", version=2)),
                encoding="utf-8")
            with_statements = plistlib.loads(Path(render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )["writer"]).read_bytes())["ProgramArguments"]
            self.assertIn("--statement-lane-governance", with_statements)
            self.assertIn(
                str((governance / "sec-financial-statements-v2.json").resolve()),
                with_statements,
            )
            # INT1 / P11a: the price lane is switched on by the same thing --
            # its own record being on disk -- and a Core without one renders
            # the plist it always rendered.
            self.assertNotIn("--market-price-governance", with_statements)
            (governance / "yfinance-daily-prices-v1.json").write_text(
                "{}", encoding="utf-8")
            with_prices = plistlib.loads(Path(render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )["writer"]).read_bytes())["ProgramArguments"]
            self.assertEqual(
                with_prices[with_prices.index("--market-price-governance") + 1],
                str((governance / "yfinance-daily-prices-v1.json").resolve()),
            )
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            raw = json.loads(config.read_text(encoding="utf-8"))
            review = raw["control"]["config"]["research_review"]
            review["document_extraction_model_config_path"] = str(state_dir / "extract.json")
            config.write_text(json.dumps(raw), encoding="utf-8")
            without = plistlib.loads(Path(render(
                root / "LaunchAgents", root / "venv" / "bin", state_dir,
                config, root / "logs")["writer"]).read_bytes())["ProgramArguments"]
            self.assertIn("--document-extraction-model-config", without)
            self.assertNotIn("--initial-screen-model-config", without)
            (state_dir / "initial-screen-model-config.json").write_text("{}", encoding="utf-8")
            with_it = plistlib.loads(Path(render(
                root / "LaunchAgents", root / "venv" / "bin", state_dir,
                config, root / "logs")["writer"]).read_bytes())["ProgramArguments"]
            self.assertEqual(
                with_it[with_it.index("--initial-screen-model-config") + 1],
                str((state_dir / "initial-screen-model-config.json").resolve()),
            )
            self.assertIn("--sec-lane-user-agent", writer_args)
            self.assertEqual(
                writer_args[writer_args.index("--candidate-staging") + 1],
                str(service.control.research_review.candidate_staging_path),
            )
            self.assertEqual(
                writer_args[writer_args.index("--candidate-staging") + 1],
                str(root / "candidate-staging.sqlite"),
            )

            self.assertNotIn("--document-extraction-model-config", writer_args)
            raw = json.loads(config.read_text())
            extraction = str(root / "approved-extraction.json")
            raw["control"]["config"]["research_review"]["document_extraction_model_config_path"] = extraction
            config.write_text(json.dumps(raw))
            paths = render(root / "LaunchAgents", root / "venv" / "bin", root / "state", config, root / "logs")
            writer_args = plistlib.loads(Path(paths["writer"]).read_bytes())["ProgramArguments"]
            self.assertEqual(writer_args[writer_args.index("--document-extraction-model-config") + 1], extraction)
            control_args = plistlib.loads(Path(paths["control"]).read_bytes())["ProgramArguments"]
            self.assertNotIn("--document-extraction-model-config", control_args)

    def test_writer_without_control_plane_has_no_candidate_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(root / "heartbeat.json"),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
            }), encoding="utf-8")
            paths = render(
                root / "LaunchAgents", root / "venv" / "bin", root / "state",
                config, root / "logs",
            )
            writer = plistlib.loads(Path(paths["writer"]).read_bytes())
            self.assertNotIn("--candidate-staging", writer["ProgramArguments"])
            self.assertNotIn("--sec-lane-governance", writer["ProgramArguments"])
            self.assertIn("--connector-governance", writer["ProgramArguments"])
            self.assertNotIn("control", paths)

    def test_bootstrap_installs_embedded_review_principal_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            config = root / "config.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(state / "core.sqlite"),
                "scheduler_db": str(state / "scheduler.sqlite"),
                "projection_db": str(state / "dashboard-projection.sqlite"),
                "model_router_db": str(state / "model-router.sqlite"),
                "capability_catalog_db": None,
                "heartbeat_path": str(state / "run" / "heartbeat.json"),
                "writer_socket": str(state / "run" / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
                "control": {"enabled": True, "config": {
                    "host": "127.0.0.1", "port": 8793,
                    "tailscale_host": "dalton.example.ts.net",
                    "tailscale_executable": "/usr/bin/true",
                    "allowed_tailscale_logins": ["owner@example.com"],
                    "writer_socket": str(state / "run" / "writer.sock"),
                    "token_config": str(state / "writer-tokens.json"),
                    "endpoint_ref": "openclaw:discord:test",
                    "feedback_timeout_seconds": 86400,
                    "sweep_interval_seconds": 60,
                    "research_review": {
                        "candidate_staging_path": str(
                            state / "candidate-staging.sqlite"
                        ),
                        "transcript_review_directory": str(
                            state / "review-inbox"
                        ),
                        "reconcile_interval_seconds": 60,
                    },
                }},
            }), encoding="utf-8")
            result = bootstrap(state, config)
            principals = load_principals(result["token_config"])
            review = principals["research-review-control"]
            self.assertEqual(
                review.operations, RESEARCH_REVIEW_CONTROL_OPERATIONS
            )
            self.assertEqual(review.actor_ref, "bridge:tailscale-review")
            dashboard = principals["dashboard-control"]
            self.assertEqual(
                dashboard.operations, DASHBOARD_CONTROL_OPERATIONS
            )
            self.assertEqual(
                dashboard.actor_ref, "bridge:tailscale-dashboard"
            )
            self.assertNotIn("human-governance", principals)

            token_config = Path(result["token_config"])
            legacy = json.loads(token_config.read_text(encoding="utf-8"))
            for entry in legacy["principals"]:
                if entry["principal_id"] == "dashboard-control":
                    dashboard_token = entry["token"]
                    entry["operations"] = [
                        "list_agenda_feedback_targets",
                        "record_agenda_feedback",
                    ]
            token_config.write_text(
                json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WriterServerError):
                load_principals(token_config)

            bootstrap(state, config)
            migrated = load_principals(token_config)["dashboard-control"]
            self.assertEqual(migrated.token, dashboard_token)
            self.assertEqual(migrated.operations, DASHBOARD_CONTROL_OPERATIONS)

            unauthorized = json.loads(token_config.read_text(encoding="utf-8"))
            for entry in unauthorized["principals"]:
                if entry["principal_id"] == "dashboard-control":
                    entry["operations"].append("commit")
            token_config.write_text(
                json.dumps(unauthorized, sort_keys=True, separators=(",", ":"))
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(WriterServerError):
                bootstrap(state, config)

    def test_health_rejects_degraded_controller_even_with_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("core.sqlite", "scheduler.sqlite", "projection.sqlite"):
                (root / name).write_bytes(b"placeholder")
            heartbeat = root / "heartbeat.json"
            heartbeat.write_text(json.dumps({
                "state": "degraded",
                "pid": 99999999,
                "last_tick_at": "2026-08-14T00:00:00+00:00",
                "plugins": {"static_dashboard": {"state": "ready"}},
            }))
            config = root / "service.json"
            config.write_text(json.dumps({
                "schema_version": "0.1",
                "core_db": str(root / "core.sqlite"),
                "scheduler_db": str(root / "scheduler.sqlite"),
                "projection_db": str(root / "projection.sqlite"),
                "model_router_db": None,
                "capability_catalog_db": None,
                "heartbeat_path": str(heartbeat),
                "writer_socket": str(root / "writer.sock"),
                "tick_seconds": 1,
                "projection_min_interval_seconds": 1,
                "plugin_retry_seconds": 1,
                "plugins": [],
            }))
            result = check(config, max_age_seconds=10**9)
            self.assertFalse(result["ok"])
            self.assertFalse(result["checks"]["controller_state_running"])

    def test_health_reports_child_settlement_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heartbeat = root / "heartbeat.json"
            fake_config = mock.Mock(
                heartbeat_path=heartbeat, tick_seconds=1,
                writer_socket=root / "writer.sock", control=None,
                core_db=root / "core.sqlite", scheduler_db=root / "scheduler.sqlite",
                projection_db=root / "projection.sqlite", bounded_planner=mock.Mock(),
            )
            for state, expected in (
                ("pending", True), ("idle", True), ("running", True),
                ("settled", True), ("unavailable", False), ("error", False),
            ):
                with self.subTest(state=state):
                    heartbeat.write_text(json.dumps({
                        "state": "running",
                        "pid": 99999999,
                        "last_tick_at": "2026-09-12T00:00:00+00:00",
                        "plugins": {},
                        "bounded_planner": {
                            "child_settlement": {"state": state},
                        },
                    }))
                    with mock.patch(
                        "dalton_core.health.ServiceConfig.from_file",
                        return_value=fake_config,
                    ):
                        result = check(root / "service.json", max_age_seconds=10**9)
                    self.assertIs(
                        result["checks"]["child_settlement_healthy"], expected)

    def test_health_fails_closed_for_missing_or_malformed_settlement_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            heartbeat = root / "heartbeat.json"
            fake_config = mock.Mock(
                heartbeat_path=heartbeat, tick_seconds=1,
                writer_socket=root / "writer.sock", control=None,
                core_db=root / "core.sqlite", scheduler_db=root / "scheduler.sqlite",
                projection_db=root / "projection.sqlite", bounded_planner=mock.Mock(),
            )
            for planner in (None, "invalid", [], {}, {"child_settlement": None}):
                with self.subTest(planner=planner):
                    heartbeat.write_text(json.dumps({
                        "state": "running", "pid": 99999999,
                        "last_tick_at": "2026-09-12T00:00:00+00:00",
                        "plugins": {}, "bounded_planner": planner,
                    }))
                    with mock.patch(
                        "dalton_core.health.ServiceConfig.from_file",
                        return_value=fake_config,
                    ):
                        result = check(root / "service.json", max_age_seconds=10**9)
                    self.assertFalse(result["checks"]["child_settlement_healthy"])

            fake_config.bounded_planner = None
            with mock.patch(
                "dalton_core.health.ServiceConfig.from_file", return_value=fake_config,
            ):
                result = check(root / "service.json", max_age_seconds=10**9)
            self.assertTrue(result["checks"]["child_settlement_healthy"])


if __name__ == "__main__":
    unittest.main()
