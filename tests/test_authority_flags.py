"""The authorization flag belongs to the connection, and every authority shares one.

``create_function`` is per-connection; the flag it read used to be per-instance.
Two authorities of one kind on one connection therefore fought over the
function, and the loser's writes failed their own trigger with a message that
reads like a caller bug and is not.
"""

from __future__ import annotations

import sqlite3
import unittest

from dalton_core.store import (
    _AUTHORIZATION_REGISTRIES,
    AuthorizationFlag,
    DaltonStore,
    authorization_flag,
)

# Every authority that guards a table with a trigger, and one table each that
# the trigger protects.  Named rather than discovered: the point of the list is
# that adding an authority without adding it here is a thing a person notices.
AUTHORITIES = (
    ("dalton_core.store", "DaltonStore", "dalton_authorized"),
    ("dalton_core.coverage_mission", "CoverageMissionAuthority",
     "dalton_coverage_mission_authorized"),
    ("dalton_core.analyst_journal", "AnalystJournalAuthority",
     "dalton_analyst_journal_authorized"),
    ("dalton_core.answer_routing", "AnswerRoutingAuthority",
     "dalton_answer_routing_authorized"),
    ("dalton_core.claim_retirement", "ClaimRetirementAuthority",
     "dalton_claim_retirement_authorized"),
    ("dalton_core.company_dossier", "CompanyDossierAuthority",
     "dalton_company_dossier_authorized"),
    ("dalton_core.deep_insight_gate", "DeepInsightGateAuthority",
     "dalton_deep_insight_gate_authorized"),
    ("dalton_core.forecast_reconciliation", "ForecastReconciliationAuthority",
     "dalton_forecast_reconciliation_authorized"),
    ("dalton_core.industry_research", "IndustryResearchAuthority",
     "dalton_industry_research_authorized"),
    ("dalton_core.mission_deliverable", "MissionDeliverableAuthority",
     "dalton_mission_deliverable_authorized"),
    ("dalton_core.model_forecast", "ModelForecastAuthority",
     "dalton_model_forecast_authorized"),
    ("dalton_core.model_forecast_driver", "ForecastModelAuthority",
     "dalton_forecast_model_authorized"),
    ("dalton_core.model_input", "ModelInputLedger",
     "dalton_model_authorized"),
    ("dalton_core.research_constitution", "ResearchConstitutionAuthority",
     "dalton_research_constitution_authorized"),
    ("dalton_core.research_cycle_reflection", "ResearchCycleReflectionAuthority",
     "dalton_research_cycle_reflection_authorized"),
    ("dalton_core.research_playbook", "ResearchPlaybookAuthority",
     "dalton_research_playbook_authorized"),
    ("dalton_core.research_quality_score", "QualityScoreAuthority",
     "dalton_research_quality_authorized"),
    ("dalton_core.weekly_brief", "WeeklyBriefAuthority",
     "dalton_weekly_brief_authorized"),
)


def build(module_name: str, class_name: str, store: DaltonStore):
    import importlib

    module = importlib.import_module(module_name)
    factory = getattr(module, class_name)
    if class_name == "DaltonStore":
        return DaltonStore(":memory:", connection=store.connection)
    if class_name == "WeeklyBriefAuthority":
        from dalton_core.industry_research import IndustryResearchAuthority

        return factory(store, IndustryResearchAuthority(store))
    if class_name == "AnswerRoutingAuthority":
        from dalton_core.agenda import AgendaStore
        from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
        from dalton_core.industry_research import IndustryResearchAuthority
        from dalton_core.research_question_backlog import ResearchQuestionBacklog

        return factory(
            store, AgendaStore(store), ResearchQuestionBacklog(store),
            BoundedPlannerAuthority(store), IndustryResearchAuthority(store),
        )
    return factory(store)


class FlagPlumbingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)

    def test_two_stores_over_one_connection_share_the_flag(self):
        """The case the per-instance flag got wrong, and the per-store one too.

        ``DaltonStore(path, connection=...)`` is a supported way to build a
        second store over somebody else's connection, so keying the flag on
        the store object left the same hole one level up.
        """

        second = DaltonStore(":memory:", connection=self.store.connection)
        self.assertFalse(self.store._authorized)
        second._authorized = True
        self.assertTrue(self.store._authorized)
        self.assertEqual(
            self.store.connection.execute("SELECT dalton_authorized()").fetchone()[0], 1)
        second._authorized = False
        self.assertFalse(self.store._authorized)
        self.assertEqual(
            self.store.connection.execute("SELECT dalton_authorized()").fetchone()[0], 0)

    def test_the_registry_is_keyed_through_the_connection_not_the_object(self):
        first = authorization_flag(self.store.connection, "dalton_test_a")
        second = authorization_flag(self.store.connection, "dalton_test_a")
        third = authorization_flag(self.store.connection, "dalton_test_b")
        self.assertIsNot(first, second)
        first.authorized = True
        self.assertTrue(second.authorized)
        # Different names on one connection are different bits.
        self.assertFalse(third.authorized)
        self.assertEqual(
            self.store.connection.execute("SELECT dalton_test_a()").fetchone()[0], 1)
        self.assertEqual(
            self.store.connection.execute("SELECT dalton_test_b()").fetchone()[0], 0)

    def test_a_second_connection_gets_its_own_registry(self):
        other = sqlite3.connect(":memory:")
        self.addCleanup(other.close)
        here = authorization_flag(self.store.connection, "dalton_test_c")
        there = authorization_flag(other, "dalton_test_c")
        here.authorized = True
        self.assertFalse(there.authorized)
        self.assertEqual(other.execute("SELECT dalton_test_c()").fetchone()[0], 0)

    def test_closing_a_store_drops_its_registry(self):
        store = DaltonStore(":memory:")
        token = store._authorization_flag._token
        self.assertIn(token, _AUTHORIZATION_REGISTRIES)
        store.close()
        self.assertNotIn(token, _AUTHORIZATION_REGISTRIES)

    def test_the_flag_is_a_view_and_reads_truthy(self):
        flag = authorization_flag(self.store.connection, "dalton_test_d")
        self.assertIsInstance(flag, AuthorizationFlag)
        self.assertFalse(flag)
        flag.authorized = 1
        self.assertTrue(flag)
        self.assertIs(flag.authorized, True)


class TwoOfEachKindTests(unittest.TestCase):
    """Two authorities of one kind on one store, and both can write.

    Before the shared flag the second one's ``create_function`` took the
    connection's function with it, so the first one's next write raised
    ``... insert requires XAuthority`` from its own trigger.
    """

    def test_every_authority_kind_survives_a_second_instance(self):
        for module_name, class_name, function_name in AUTHORITIES:
            with self.subTest(authority=class_name):
                store = DaltonStore(":memory:")
                try:
                    first = build(module_name, class_name, store)
                    second = build(module_name, class_name, store)
                    connection = store.connection
                    self.assertEqual(
                        connection.execute(f"SELECT {function_name}()").fetchone()[0], 0)
                    # Whichever one is inside its transaction is the one that
                    # is authorized, and neither can switch the other off.
                    first._authorized = True
                    self.assertTrue(second._authorized)
                    self.assertEqual(
                        connection.execute(f"SELECT {function_name}()").fetchone()[0], 1)
                    first._authorized = False
                    second._authorized = True
                    self.assertTrue(first._authorized)
                    self.assertEqual(
                        connection.execute(f"SELECT {function_name}()").fetchone()[0], 1)
                    second._authorized = False
                    self.assertEqual(
                        connection.execute(f"SELECT {function_name}()").fetchone()[0], 0)
                finally:
                    store.close()


class PathAuthorityTests(unittest.TestCase):
    """The two that own a connection instead of borrowing a store's.

    ``ModelRouter`` and ``Scheduler`` take a path or a connection, so the
    "second instance on one connection" case is reached by handing the second
    one the first one's connection -- which is what the writer does when it
    reopens a lane's scheduler.
    """

    CASES = (
        ("dalton_core.model_router", "ModelRouter", "dalton_model_router_authorized"),
        ("dalton_core.scheduler", "Scheduler", "dalton_scheduler_authorized"),
    )

    def test_two_of_each_share_the_flag_and_a_bare_write_is_refused(self):
        import importlib

        for module_name, class_name, function_name in self.CASES:
            with self.subTest(authority=class_name):
                factory = getattr(importlib.import_module(module_name), class_name)
                first = factory(":memory:")
                second = factory(":memory:", connection=first.connection)
                connection = first.connection
                self.assertEqual(
                    connection.execute(f"SELECT {function_name}()").fetchone()[0], 0)
                first._authorized = True
                self.assertTrue(second._authorized)
                self.assertEqual(
                    connection.execute(f"SELECT {function_name}()").fetchone()[0], 1)
                first._authorized = False
                second._authorized = True
                self.assertTrue(first._authorized)
                self.assertEqual(
                    connection.execute(f"SELECT {function_name}()").fetchone()[0], 1)
                second._authorized = False
                guarded = sorted({
                    row["tbl_name"] for row in connection.execute(
                        "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger'"
                    ).fetchall()
                    if row["sql"] and f"{function_name}()" in row["sql"]
                })
                self.assertTrue(guarded, f"{class_name} guards nothing")
                for table in guarded:
                    columns = [
                        row[1] for row in
                        connection.execute(f"PRAGMA table_info({table})").fetchall()
                    ]
                    placeholders = ",".join("?" for _ in columns)
                    with self.assertRaises(sqlite3.DatabaseError):
                        connection.execute(
                            f"INSERT INTO {table} VALUES({placeholders})",
                            ["x" for _ in columns],
                        )
                connection.close()


class BareConnectionTests(unittest.TestCase):
    def guarded_tables(self, connection, function_name):
        """Every table whose insert trigger calls this authority's function.

        Discovered rather than listed: a table that gains a guard should be
        covered by this test the day it gains one, and a list would have to be
        remembered.
        """

        return sorted({
            row["tbl_name"] for row in connection.execute(
                "SELECT tbl_name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
            if row["sql"] and f"{function_name}()" in row["sql"]
        })

    def test_every_guarded_table_refuses_a_bare_connection(self):
        """The trigger, not the authority, is what makes a table append-only.

        Opening the authority installs the schema and leaves the flag off, so
        an INSERT that does not go through a transaction is refused whatever
        the caller believes about its own permissions.
        """

        checked = 0
        for module_name, class_name, function_name in AUTHORITIES:
            store = DaltonStore(":memory:")
            try:
                build(module_name, class_name, store)
                connection = store.connection
                self.assertEqual(
                    connection.execute(f"SELECT {function_name}()").fetchone()[0], 0,
                    "the flag must be off outside a transaction")
                tables = self.guarded_tables(connection, function_name)
                self.assertTrue(tables, f"{class_name} guards nothing")
                for table in tables:
                    with self.subTest(table=table):
                        columns = [
                            row[1] for row in
                            connection.execute(f"PRAGMA table_info({table})").fetchall()
                        ]
                        placeholders = ",".join("?" for _ in columns)
                        with self.assertRaises(sqlite3.DatabaseError):
                            connection.execute(
                                f"INSERT INTO {table} VALUES({placeholders})",
                                ["x" for _ in columns],
                            )
                        checked += 1
            finally:
                store.close()
        self.assertGreater(checked, 30)

    def test_the_reopen_ledger_is_one_of_them(self):
        # Named on its own because it is the newest guarded table and the one
        # this branch's predecessor added.
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        from dalton_core.coverage_mission import CoverageMissionAuthority

        CoverageMissionAuthority(store)
        self.assertIn(
            "coverage_mission_stage_reopens",
            self.guarded_tables(store.connection, "dalton_coverage_mission_authorized"),
        )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "requires CoverageMissionAuthority"):
            store.connection.execute(
                "INSERT INTO coverage_mission_stage_reopens(record_id,mission_version_ref,"
                "company_ref,stage_ref,reopen_decision_ref,reopen_proposal_ref,"
                "reopened_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES('r','m','c','initial_screen','d','p','v','{}','h','human:x','2026')"
            )


if __name__ == "__main__":
    unittest.main()
