"""The decisions inside the deploy rehearsal, tested without a deploy.

``scripts/rehearse_deploy.py`` copies 800 MB and forks a writer, so the run
itself is not a unit test.  What *is* testable is everything it decides before
it touches anything: which files travel, how a live path becomes a temp one,
what ``install.sh`` actually seeds, which schema files have a known owner, how
two plists differ, and which lane statuses mean "held" rather than "escaped".

The seed test is the one that earns its keep: ``INSTALL_SEEDS`` is a second
copy of a list that lives in a shell script, and a second copy is a copy that
drifts.  It has drifted once already -- ``sec-filings-index-v1.json`` is on the
live Core and in no repository -- which is exactly the failure this pins.
"""

from __future__ import annotations

import importlib
import re
import tempfile
import unittest
from pathlib import Path

from scripts.rehearse_deploy import (
    CORE_MIGRATIONS,
    INSTALL_SEEDS,
    LANE_SWITCHES,
    REPO_ROOT,
    REQUIRED_WRITE_SCOPES,
    SIDECAR_MIGRATIONS,
    CROWD_TOOL_VARS,
    GATES,
    CopyItem,
    LaneRow,
    LaneSwitch,
    copy_excluded,
    copy_plan,
    escaped,
    escaped_rows,
    gate_open,
    invert,
    known_schema_files,
    lane_rows,
    main,
    missing_lane_switches,
    missing_write_scopes,
    normalise_plist,
    orphan_live_records,
    package_schema_files,
    path_replacements,
    plist_diff,
    reason_of,
    render_table,
    rewrite_paths,
    committed_governance_records,
    deliberately_unseeded_records,
    expand_for_loops,
    install_script_code,
    install_seeded_records,
    seeded_repo_records,
    status_of,
    strip_shell_comments,
    unseeded_governance_records,
)


INSTALL_SH = REPO_ROOT / "deploy" / "macos" / "install.sh"


def _install_script_code() -> str:
    """``install.sh`` with its comments removed and its seed loops expanded.

    Two transformations, each for a failure this test has already had.

    Comments go because half the connector names in that file appear only in a
    comment explaining why they are *not* seeded, so matching against the raw
    text would report every deliberately-absent lane as installed.

    Loops are expanded because the newer seed blocks are written as
    ``for cn_hk_kind in financial-statements shareholders ...; do`` with the
    filename built from ``${cn_hk_kind}``, so the literal name appears nowhere
    in the script.  Without the expansion the reverse check silently *skipped*
    those records rather than failing on them, and six records that install.sh
    genuinely seeds sat outside ``INSTALL_SEEDS`` with the suite green.  A test
    whose gap is invisible is worse than no test.

    Both live in ``rehearse_deploy`` rather than here, because the rehearsal
    itself now reads its seed set out of the script and two expanders that
    disagree would be exactly the drift this is about.
    """

    return install_script_code(INSTALL_SH)


def _mentions(code: str, token: str) -> bool:
    """Whether ``install.sh`` names this connector.

    The trailing guard admits a following ``-``, because half the seeds are
    written as ``sec-financial-statements-${sec_financials_version}.json``
    inside a ``for`` loop -- the version is a shell variable, so the literal
    filename never appears anywhere in the script.  The leading guard does not
    admit one, which is what keeps ``guidepoint-get-transcript-narrowing`` from
    reading as seeded because ``guidepoint-get-transcript`` is.
    """

    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w])", code) is not None


class CopyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.live = Path(directory.name) / "live"
        self.temp = Path(directory.name) / "temp"
        state = self.live / "state" / "dalton-core"
        (state / "connector-governance").mkdir(parents=True)
        (state / "run").mkdir(parents=True)
        (state / "acquisitions" / "big").mkdir(parents=True)
        (self.live / "config").mkdir(parents=True)
        (self.live / "config" / "service.json").write_text("{}", encoding="utf-8")
        (state / "core.sqlite").write_bytes(b"")
        (state / "document-extraction-model-config.json").write_text("{}", encoding="utf-8")
        (state / "connector-governance" / "web-fetch-v1.json").write_text("{}", encoding="utf-8")
        (state / "run" / "heartbeat.json").write_text("{}", encoding="utf-8")
        (state / "run" / "writer.sock").write_bytes(b"")
        (state / "acquisitions" / "big" / "huge.pdf").write_bytes(b"x" * 1024)

    def _plan(self) -> list[CopyItem]:
        return copy_plan(self.live, self.temp)

    def test_the_service_config_is_the_one_required_item(self) -> None:
        required = [item for item in self._plan() if item.required]
        self.assertEqual(
            [item.source.name for item in required], ["service.json"],
            "everything else is optional: a Core that has never run has none of it",
        )

    def test_sqlite_databases_travel_through_the_backup_path(self) -> None:
        kinds = {item.source.name: item.kind for item in self._plan()}
        self.assertEqual(kinds["core.sqlite"], "sqlite")
        self.assertEqual(kinds["document-extraction-model-config.json"], "file")

    def test_the_document_corpus_does_not_travel(self) -> None:
        sources = {str(item.source) for item in self._plan()}
        self.assertNotIn(str(self.live / "state" / "dalton-core" / "acquisitions"), sources)

    def test_the_governance_and_plan_trees_travel_whole(self) -> None:
        trees = {
            item.source.name for item in self._plan() if item.kind == "tree"
        }
        self.assertEqual(trees, {"connector-governance", "discovery-plans", "run"})

    def test_destinations_all_land_under_the_temp_root(self) -> None:
        for item in self._plan():
            self.assertTrue(
                str(item.destination).startswith(str(self.temp)),
                f"{item.destination} escapes the temp root",
            )

    def test_a_socket_never_travels(self) -> None:
        self.assertTrue(copy_excluded(Path("run/writer.sock")))
        self.assertTrue(copy_excluded(Path(".writer-tokens.json.governance.lock")))
        self.assertFalse(copy_excluded(Path("run/heartbeat.json")))


class PathRewritingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.replacements = path_replacements(
            Path("/live/Dalton"), Path("/tmp/rehearsal"),
            broker_dir=Path("/home/me/.openclaw"),
            stub_broker_dir=Path("/tmp/rehearsal/broker"),
        )

    def test_a_nested_value_is_rewritten_everywhere(self) -> None:
        value = {
            "core_db": "/live/Dalton/state/core.sqlite",
            "planner": {"broker": "/home/me/.openclaw/dalton-model-broker.sock"},
            "list": ["/live/Dalton/run", "unrelated"],
        }
        out = rewrite_paths(value, self.replacements)
        self.assertEqual(out["core_db"], "/tmp/rehearsal/state/core.sqlite")
        self.assertEqual(
            out["planner"]["broker"], "/tmp/rehearsal/broker/dalton-model-broker.sock"
        )
        self.assertEqual(out["list"], ["/tmp/rehearsal/run", "unrelated"])

    def test_non_strings_are_left_alone(self) -> None:
        value = {"port": 8793, "enabled": True, "cap": None, "cost": 1.5}
        self.assertEqual(rewrite_paths(value, self.replacements), value)

    def test_only_a_prefix_is_replaced_not_a_substring(self) -> None:
        self.assertEqual(
            rewrite_paths("note about /live/Dalton", self.replacements),
            "note about /live/Dalton",
            "a path in the middle of prose is not a path this rewrite owns",
        )

    def test_the_longest_replacement_wins(self) -> None:
        replacements = {"/a": "/short", "/a/b": "/long"}
        self.assertEqual(rewrite_paths("/a/b/c", replacements), "/long/c")

    def test_inverting_takes_a_temp_artefact_back_to_its_live_form(self) -> None:
        live = "/live/Dalton/state/core.sqlite"
        forward = rewrite_paths(live, self.replacements)
        self.assertEqual(rewrite_paths(forward, invert(self.replacements)), live)


class InstallSeedTests(unittest.TestCase):
    """``INSTALL_SEEDS`` against the shell script it transcribes."""

    def setUp(self) -> None:
        self.code = _install_script_code()

    def test_every_seed_in_the_list_is_a_seed_in_the_script(self) -> None:
        for spec in INSTALL_SEEDS:
            stem = re.sub(r"-v\d+\.json$", "", Path(spec.repo).name)
            with self.subTest(seed=spec.repo):
                self.assertTrue(
                    _mentions(self.code, stem) or _mentions(self.code, Path(spec.repo).name),
                    f"{spec.repo} is in INSTALL_SEEDS but install.sh does not copy it",
                )

    def test_every_governance_record_the_script_seeds_is_in_the_list(self) -> None:
        # Exact, in both directions, and resolved through the ``cp`` pair
        # rather than by matching a name: a record copied to
        # ``governance-decisions/`` is not a record seeded into the runtime
        # governance directory, and the two must not read the same.
        self.assertEqual(
            install_seeded_records(INSTALL_SH),
            frozenset(
                Path(spec.repo).name for spec in INSTALL_SEEDS
                if spec.state.startswith("connector-governance/")
            ),
            "INSTALL_SEEDS and install.sh disagree about which records are "
            "seeded into the runtime governance directory",
        )

    def test_every_seed_source_is_actually_in_the_repo(self) -> None:
        """Gated or not. A gate decides whether the owner's machine wants the
        lane; it does not excuse a seed pointing at a file nobody committed,
        which is a lane that can never be installed however the owner is set
        up."""

        for spec in INSTALL_SEEDS:
            with self.subTest(seed=spec.repo):
                self.assertTrue((REPO_ROOT / spec.repo).is_file())

    def test_a_gated_seed_names_a_gate_and_an_ungated_one_does_not(self) -> None:
        for spec in INSTALL_SEEDS:
            with self.subTest(seed=spec.repo):
                self.assertEqual(bool(spec.optional), bool(spec.gate))
                if spec.gate:
                    self.assertIn(spec.gate, GATES)

    def test_the_narrowing_record_is_not_seeded_into_the_runtime_governance_dir(self) -> None:
        """install.sh is explicit that it must not be, and the reason is real:
        nothing loads it, so a permanently-proposed copy under
        ``connector-governance/`` would show the owner a lane waiting for an
        approval about nothing."""

        [spec] = [s for s in INSTALL_SEEDS if "narrowing" in s.repo]
        self.assertEqual(
            spec.state, "governance-decisions/guidepoint-get-transcript-narrowing-v1.json"
        )
        self.assertIn("governance-decisions", _install_script_code())

    def test_seed_destinations_are_relative_and_stay_inside_the_state_dir(self) -> None:
        for spec in INSTALL_SEEDS:
            with self.subTest(seed=spec.state):
                self.assertFalse(Path(spec.state).is_absolute())
                self.assertNotIn("..", Path(spec.state).parts)

    def test_every_committed_record_now_has_a_seed_path(self) -> None:
        """INT2 closed the gap this once reported.

        When this was written, ``install.sh`` seeded none of the S1 feeds, the
        crowd sources, the China/Hong Kong records or the catalyst calendar,
        and the test asserted that ``sales-notes-get-note-v1.json`` came back
        as unseeded.  INT2 added all of them, so asserting the old answer would
        now be asserting a bug.

        What is worth keeping is the invariant underneath: a governance record
        committed to the repository and seeded by nothing is a lane that reads
        ``unconfigured`` for ever without anyone having decided that.  Today
        that set is empty and the assertion is that it stays empty -- fix it by
        adding a seed block to ``install.sh`` *and* an entry to
        ``INSTALL_SEEDS``, or by deleting the record.
        """

        self.assertEqual(
            unseeded_governance_records(REPO_ROOT), (),
            "seed it in its lane's all-or-nothing block, or name it in "
            "DELIBERATELY_UNSEEDED in install.sh with the reason",
        )
        self.assertNotIn("web-fetch-v1.json", unseeded_governance_records(REPO_ROOT))

    def test_the_two_sets_cover_the_repo_exactly_and_do_not_overlap(self) -> None:
        # INT3: the invariant above is only worth anything if "deliberately
        # not seeded" is a set the script declares rather than a comment
        # somebody wrote. A record cannot be in both, and the array cannot
        # name a record the repository does not carry.
        committed = committed_governance_records(REPO_ROOT)
        seeded = install_seeded_records(INSTALL_SH)
        named = deliberately_unseeded_records(INSTALL_SH)
        self.assertEqual(seeded | named, committed)
        self.assertEqual(seeded & named, frozenset())
        self.assertEqual(named - committed, frozenset())

    def test_the_deliberate_absences_are_the_ones_that_were_decided(self) -> None:
        named = deliberately_unseeded_records(INSTALL_SH)
        # roic.ai answers 403 site-wide since 2026-08-29 and nothing in the
        # writer loads either record, so seeding them would be two approvals
        # to make about a source that answers nothing.
        self.assertIn("roic-list-transcripts-v1.json", named)
        self.assertIn("roic-get-transcript-v1.json", named)
        # The narrowing note is seeded, but into governance-decisions/, so it
        # is not an approval put in front of the owner.
        self.assertIn("guidepoint-get-transcript-narrowing-v1.json", named)

    def test_the_calendar_record_is_seeded(self) -> None:
        # The one that mattered: C1's lane was a single block away and the
        # record had been committed for a day.
        self.assertIn("yfinance-calendar-v1.json", install_seeded_records(INSTALL_SH))

    def test_a_loop_written_seed_is_read_as_a_seed(self) -> None:
        seeded = install_seeded_records(INSTALL_SH)
        for kind in ("financial-statements", "shareholders", "buybacks",
                     "margin-balance", "northbound-flow", "ah-premium"):
            self.assertIn(f"cn-hk-findata-{kind}-v1.json", seeded)

    def test_the_loop_expander_handles_a_continued_word_list(self) -> None:
        code = ('for k in a b \\\n        c; do\n'
                '  f="$governance_dir/${k}-v1.json"\n'
                '  cp "$repo_root/deploy/connector-governance/${k}-v1.json" "$f"\n'
                'done\n')
        expanded = expand_for_loops(code)
        for word in ("a", "b", "c"):
            self.assertIn(f"{word}-v1.json", expanded)
        self.assertNotIn("${k}", expanded)

    def test_a_comment_naming_a_record_is_not_a_seed(self) -> None:
        raw = INSTALL_SH.read_text(encoding="utf-8")
        self.assertIn("roic-list-transcripts-v1.json", raw)
        self.assertNotIn(
            "roic-list-transcripts-v1.json",
            strip_shell_comments(raw).split("DELIBERATELY_UNSEEDED", 1)[0],
        )

    def test_the_six_china_records_are_seeded_even_though_no_lane_reads_them(self) -> None:
        """They are ungated on purpose: with no lane there is nothing to
        half-install, and the owner still has six schema hashes to approve
        separately."""

        china = [s for s in INSTALL_SEEDS if "cn-hk-findata" in s.repo]
        self.assertEqual(len(china), 6)
        for spec in china:
            with self.subTest(seed=spec.repo):
                self.assertFalse(spec.optional)
                self.assertTrue(spec.state.startswith("connector-governance/"))

    def test_a_live_record_with_no_committed_source_is_named(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        governance = Path(directory.name)
        (governance / "web-fetch-v1.json").write_text("{}", encoding="utf-8")
        (governance / "not-in-any-repository-v1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            orphan_live_records(governance, REPO_ROOT),
            ("not-in-any-repository-v1.json",),
        )

    def test_the_filings_index_record_is_no_longer_an_orphan(self) -> None:
        # INT3: it existed only on the live Core while the writer's plist named
        # it unconditionally, so a rebuilt Core got a dead path. It is now
        # committed *and* seeded -- either alone is worth nothing.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        governance = Path(directory.name)
        (governance / "sec-filings-index-v1.json").write_text("{}", encoding="utf-8")
        self.assertEqual(orphan_live_records(governance, REPO_ROOT), ())
        self.assertIn(
            "sec-filings-index-v1.json", install_seeded_records(INSTALL_SH))

    def test_an_absent_governance_directory_is_not_an_error(self) -> None:
        self.assertEqual(orphan_live_records(Path("/nonexistent"), REPO_ROOT), ())


class SeedGateTests(unittest.TestCase):
    """The gates decide whether the rehearsal installs a lane at all."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name) / "workspace"
        self.env = {"DALTON_OPENCLAW_WORKSPACE": str(self.workspace)}

    def test_an_ungated_seed_is_always_open(self) -> None:
        self.assertEqual(gate_open("", {}), (True, ""))

    def test_the_market_digest_gate_wants_the_output_directory(self) -> None:
        is_open, why = gate_open("market-digest", self.env)
        self.assertFalse(is_open)
        self.assertIn("market-digest", why)
        (self.workspace / "skills" / "market-digest" / "output").mkdir(parents=True)
        self.assertEqual(gate_open("market-digest", self.env)[0], True)

    def test_the_company_wiki_gate_wants_the_index_file(self) -> None:
        self.workspace.mkdir(parents=True)
        self.assertFalse(gate_open("company-wiki", self.env)[0])
        (self.workspace / "wiki-index.sqlite").write_bytes(b"")
        self.assertTrue(gate_open("company-wiki", self.env)[0])

    def test_the_feed_plan_gate_is_the_union_of_the_two_feed_lanes(self) -> None:
        self.workspace.mkdir(parents=True)
        self.assertFalse(gate_open("any-feed", self.env)[0])
        (self.workspace / "wiki-index.sqlite").write_bytes(b"")
        self.assertTrue(
            gate_open("any-feed", self.env)[0],
            "either feed lane seeds the shared plan, so one is enough",
        )

    def test_the_crowd_gate_names_the_variables_that_are_not_set(self) -> None:
        is_open, why = gate_open("crowd-tools", {})
        self.assertFalse(is_open)
        for name in CROWD_TOOL_VARS:
            self.assertIn(name, why)

    def test_the_crowd_gate_wants_all_three_tools_not_some(self) -> None:
        tool = Path(tempfile.mkdtemp(dir="/tmp")) / "agent-reach"
        self.addCleanup(lambda: tool.unlink(missing_ok=True))
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        tool.chmod(0o700)
        env = {name: str(tool) for name in CROWD_TOOL_VARS[:2]}
        self.assertFalse(gate_open("crowd-tools", env)[0])
        env[CROWD_TOOL_VARS[2]] = str(tool)
        self.assertTrue(gate_open("crowd-tools", env)[0])

    def test_a_variable_naming_something_unexecutable_does_not_open_the_gate(self) -> None:
        plain = Path(tempfile.mkdtemp(dir="/tmp")) / "not-a-tool"
        self.addCleanup(lambda: plain.unlink(missing_ok=True))
        plain.write_text("", encoding="utf-8")
        plain.chmod(0o600)
        env = {name: str(plain) for name in CROWD_TOOL_VARS}
        self.assertFalse(gate_open("crowd-tools", env)[0])

    def test_every_gate_the_seeds_name_is_implemented(self) -> None:
        used = {spec.gate for spec in INSTALL_SEEDS if spec.gate}
        self.assertEqual(used - set(GATES), set())
        self.assertEqual(
            set(GATES) - used, set(), "an unused gate is a gate nobody checks"
        )


class MigrationCoverageTests(unittest.TestCase):
    def test_every_shipped_schema_has_a_named_owner(self) -> None:
        package = Path(importlib.import_module("dalton_core").__file__).parent
        missing = package_schema_files(package) - known_schema_files()
        self.assertEqual(
            missing, frozenset(),
            "a new *_schema.sql needs a MigrationSpec, or the rehearsal will "
            "report a deploy as clean without ever having run that migration",
        )

    def test_the_owner_list_names_nothing_that_is_not_shipped(self) -> None:
        package = Path(importlib.import_module("dalton_core").__file__).parent
        self.assertEqual(known_schema_files() - package_schema_files(package), frozenset())

    def test_every_migration_symbol_imports(self) -> None:
        for spec in CORE_MIGRATIONS + SIDECAR_MIGRATIONS:
            with self.subTest(schema=spec.schema):
                module = importlib.import_module(spec.module)
                self.assertTrue(hasattr(module, spec.symbol))

    def test_core_migrations_run_against_the_core_database(self) -> None:
        for spec in CORE_MIGRATIONS:
            with self.subTest(schema=spec.schema):
                self.assertEqual(spec.database, "core.sqlite")

    def test_the_two_migrations_this_deploy_actually_changes_are_present(self) -> None:
        schemas = {spec.schema for spec in CORE_MIGRATIONS + SIDECAR_MIGRATIONS}
        self.assertIn("budget_pools_schema.sql", schemas, "C2's nullable pool column")
        self.assertIn("mission_deliverable_schema.sql", schemas, "P14a's CHECK widening")
        self.assertIn("tick_ledger_schema.sql", schemas)
        self.assertIn("claim_index_schema.sql", schemas)


class PlistDiffTests(unittest.TestCase):
    def test_an_added_argument_is_reported_by_name(self) -> None:
        before = {"ProgramArguments": ["dalton-writer", "--db", "/x"]}
        after = {"ProgramArguments": ["dalton-writer", "--db", "/x", "--reflection-lane"]}
        self.assertEqual(plist_diff(before, after), ["ProgramArguments: +--reflection-lane"])

    def test_a_removed_argument_is_reported_by_name(self) -> None:
        before = {"ProgramArguments": ["a", "--gone"]}
        after = {"ProgramArguments": ["a"]}
        self.assertEqual(plist_diff(before, after), ["ProgramArguments: --gone".replace(": ", ": -")])

    def test_reordering_alone_is_still_reported(self) -> None:
        before = {"ProgramArguments": ["a", "b"]}
        after = {"ProgramArguments": ["b", "a"]}
        self.assertEqual(
            plist_diff(before, after),
            ["ProgramArguments: same arguments, different order"],
        )

    def test_an_identical_plist_has_no_diff(self) -> None:
        value = {"Label": "x", "ProgramArguments": ["a"], "KeepAlive": True}
        self.assertEqual(plist_diff(value, dict(value)), [])

    def test_an_absent_key_reads_as_absent_rather_than_null(self) -> None:
        self.assertEqual(plist_diff({}, {"StartInterval": 300}),
                         ["StartInterval: <absent> -> 300"])
        self.assertEqual(plist_diff({"StartInterval": 300}, {}),
                         ["StartInterval: 300 -> <absent>"])

    def test_normalising_hides_the_root_and_leaves_the_real_change(self) -> None:
        replacements = {"/tmp/rehearsal": "/live/Dalton"}
        live = {"ProgramArguments": ["--db", "/live/Dalton/core.sqlite"]}
        rendered = {"ProgramArguments": ["--db", "/tmp/rehearsal/core.sqlite", "--new"]}
        self.assertEqual(
            plist_diff(live, normalise_plist(rendered, replacements)),
            ["ProgramArguments: +--new"],
        )


class TickReadingTests(unittest.TestCase):
    OPERATIONS = {
        "mission_market_prices": "dispatch_mission_market_prices",
        "claim_index": "dispatch_claim_index",
        "research_plan": "dispatch_research_plan",
    }

    def test_a_lane_that_declined_is_held(self) -> None:
        for status in ("ungranted", "unconfigured", "unapproved", "idle", "held",
                       "deferred", "launched", "skipped:pool_exhausted"):
            with self.subTest(status=status):
                self.assertFalse(escaped(status))

    def test_an_exception_that_got_out_is_not_held(self) -> None:
        self.assertTrue(escaped("unavailable:PermissionError"))
        self.assertTrue(escaped("unrecorded:OperationalError"))

    def test_the_table_reports_one_row_per_registered_lane(self) -> None:
        summary = {
            "mission_market_prices": {"status": "ungranted", "reason": "no market_price"},
            "claim_index": {"status": "unavailable:KeyError"},
            "research_plan": {"status": "idle"},
            "tick_ledger": {"status": "recorded", "lane_count": 3},
        }
        rows = lane_rows(summary, self.OPERATIONS)
        self.assertEqual(
            [(row.lane, row.status, row.held) for row in rows],
            [
                ("mission_market_prices", "ungranted", True),
                ("claim_index", "unavailable:KeyError", False),
                ("research_plan", "idle", True),
                ("tick_ledger", "recorded", True),
            ],
        )
        self.assertEqual([row.lane for row in escaped_rows(rows)], ["claim_index"])

    def test_a_lane_the_tick_never_reported_is_named_rather_than_dropped(self) -> None:
        rows = lane_rows({}, self.OPERATIONS)
        self.assertEqual({row.status for row in rows}, {"<not in tick>"})

    def test_a_lane_result_that_is_not_a_mapping_does_not_crash_the_table(self) -> None:
        rows = lane_rows({"research_plan": None}, {"research_plan": "dispatch_research_plan"})
        self.assertEqual(rows[0].status, "<NoneType>")
        self.assertEqual(status_of("oops"), "<str>")

    def test_the_reason_falls_back_to_the_lane_s_counts(self) -> None:
        self.assertEqual(reason_of({"status": "idle", "scanned": 3, "read": 1}),
                         "read=1 scanned=3")
        self.assertEqual(reason_of({"status": "idle", "reason": "nothing due"}),
                         "nothing due")
        self.assertEqual(reason_of({"status": "idle"}), "")
        self.assertEqual(reason_of({"status": "idle", "done": True}), "",
                         "a boolean is not a count")

    def test_the_table_renders_aligned_with_a_header(self) -> None:
        rendered = render_table([
            LaneRow("claim_index", "dispatch_claim_index", "ungranted", "no word", True),
        ])
        lines = rendered.splitlines()
        self.assertEqual(lines[0].split(), ["lane", "status", "reason"])
        self.assertTrue(set(lines[1]) <= {"-", " "})
        self.assertIn("claim_index", lines[2])

    def test_an_empty_table_still_has_a_header(self) -> None:
        self.assertEqual(render_table([]).splitlines()[0].split(),
                         ["lane", "status", "reason"])


class MissionAndSwitchTests(unittest.TestCase):
    def test_a_mission_granting_everything_leaves_nothing_missing(self) -> None:
        granted = [word for word, _ in REQUIRED_WRITE_SCOPES]
        self.assertEqual(missing_write_scopes(granted), [])

    def test_the_live_grant_list_is_reported_with_a_reason_for_each_word(self) -> None:
        missing = missing_write_scopes(["evidence", "claim", "forecast_line"])
        words = [word for word, _ in missing]
        self.assertIn("market_price", words)
        self.assertIn("claim_index", words)
        for _, why in missing:
            self.assertTrue(why, "a missing grant without a reason is a to-do, not a finding")

    def test_every_required_scope_is_a_word_the_mission_vocabulary_knows(self) -> None:
        from dalton_core.coverage_mission import AUTOMATION_WRITE_SCOPES

        for word, _ in REQUIRED_WRITE_SCOPES:
            with self.subTest(word=word):
                self.assertIn(
                    word, AUTOMATION_WRITE_SCOPES,
                    "a mission version cannot grant a word the code does not know",
                )

    def test_a_lane_switch_on_disk_is_not_reported_missing(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        for switch in LANE_SWITCHES:
            (state / switch.state_file).write_text("{}", encoding="utf-8")
        self.assertEqual(missing_lane_switches(state), ())

    def test_an_empty_state_reports_every_switch(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertEqual(len(missing_lane_switches(Path(directory.name))), len(LANE_SWITCHES))

    def test_a_switch_that_names_a_repo_source_names_a_file_that_exists(self) -> None:
        for switch in LANE_SWITCHES:
            if switch.repo_source is None:
                continue
            with self.subTest(lane=switch.lane):
                self.assertTrue((REPO_ROOT / switch.repo_source).is_file())

    def test_the_switch_install_sh_does_write_is_the_one_it_names(self) -> None:
        # Three now: extraction's model config, P14a's tracking policy, and
        # P14-M2's catalog-sync switch. Each has to be traceable to the line in
        # install.sh that writes it.
        seeded = {switch.state_file for switch in LANE_SWITCHES
                  if switch.seeded_by_install}
        code = _install_script_code()
        self.assertEqual(
            seeded,
            {"document-extraction-model-config.json", "tracking-policy.json",
             "model-catalog-sync.json"},
        )
        self.assertIn("document_extraction_setup", code)
        self.assertIn("p14a-tracking-policy-v1.json", code)
        self.assertIn("model-catalog-sync.json", code)
        self.assertIn("tracking-policy.json", [spec.state for spec in INSTALL_SEEDS])

    def test_the_three_model_config_switches_are_written_when_named(self) -> None:
        # INT3: the judgement pair and the claim index were switches nothing
        # wrote. They are env-gated setup calls now, on the same rule as the
        # planner and the deliverable: named, or the lane stays absent.
        code = _install_script_code()
        for name, variable in (
            ("event-judgement-model-config.json", "DALTON_EVENT_JUDGEMENT_MODEL_TIER"),
            ("event-verifier-model-config.json", "DALTON_EVENT_VERIFIER_MODEL_TIER"),
            ("claim-index-model-config.json", "DALTON_CLAIM_INDEX_MODEL_TIER"),
        ):
            with self.subTest(switch=name):
                self.assertIn(name, code)
                self.assertIn(variable, code)
                switch = next(s for s in LANE_SWITCHES if s.state_file == name)
                self.assertFalse(
                    switch.seeded_by_install,
                    "unset, install.sh writes nothing and the lane is absent",
                )


class EntryPointTests(unittest.TestCase):
    def test_a_temp_root_outside_tmp_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            main(["--temp-root", "/Users/somebody/Dalton"])
        self.assertIn("/tmp", str(caught.exception))

    def test_the_live_root_may_not_also_be_the_temp_root(self) -> None:
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        with self.assertRaises(SystemExit) as caught:
            main(["--temp-root", directory.name, "--live-root", directory.name])
        self.assertIn("must not be the live root", str(caught.exception))


class LaneSwitchRecordTests(unittest.TestCase):
    def test_a_switch_is_frozen(self) -> None:
        switch = LaneSwitch("x", "x.json", None, False)
        with self.assertRaises(Exception):
            switch.lane = "y"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
