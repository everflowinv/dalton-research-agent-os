"""S3: the three crowd children, offline, on synthetic material.

Everything here replays a fixture. No post, no review and no handle in this
file exists, and the only "credential" is a grant envelope whose every field is
the word synthetic -- which is the point of a grant envelope: it can be written
down in a test because it holds nothing.

The order the children keep is the order these tests are grouped in: approval,
then the credential slot, then the artifact, then the contract. Each is a
refusal cheaper than the one after it, and each one is tested by making the
step before it succeed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from dalton_core import employee_reviews_cli, xreach_cli, xueqiu_cli
from dalton_core.connector_governance import build_governance_record
from dalton_core.crowd_credential_grants import (
    CrowdCredentialSlotUnbound,
    load_credential_grant,
    redacted,
    require_slots,
)
from dalton_core.store import content_hash
from dalton_core.xreach_core import CREDENTIAL_SLOT_REFS
from dalton_core.xueqiu_core import CREDENTIAL_SLOT_REF
from tests.crowd_fixtures import (
    blind_page,
    credential_grant,
    write_json,
    xreach_posts,
    xueqiu_posts,
)

XUEQIU_TARGET = "host-tool:agent-reach-xueqiu-channel"
XREACH_TARGET = "host-tool:xreach"


class ChildTestCase(unittest.TestCase):
    """Temp state, a governance writer and a summary reader."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.state = self.root / "state"
        self.out = self.root / "out"
        self.state.mkdir()
        self.out.mkdir()

    def governance(self, kind: str, *, status: str = "approved",
                   **overrides: Any) -> Path:
        record = build_governance_record(kind, approved_by="human:tester",
                                         status=status)
        record.update(overrides)
        if overrides:
            # A record whose fields were edited must still be self-consistent,
            # otherwise the test proves the hash check rather than the drift.
            body = {key: value for key, value in record.items()
                    if key != "content_hash"}
            record["content_hash"] = content_hash(body)
        path = self.root / f"{kind}.json"
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return path

    def fixture(self, value: Any, name: str = "fixture.json") -> Path:
        path = self.root / name
        if isinstance(value, (bytes, bytearray)):
            path.write_bytes(bytes(value))
        else:
            write_json(path, value)
        return path

    def summary(self) -> dict[str, Any]:
        return json.loads((self.out / "summary.json").read_text(encoding="utf-8"))


class XueqiuChildTests(ChildTestCase):
    def run_child(self, *, operation="search_posts", governance=None,
                  grant: Path | None = None, fixture=None, extra=()) -> int:
        argv = [
            "--state-dir", str(self.state), "--summary-dir", str(self.out),
            "--governance", str(governance or self.governance("xueqiu-search-posts")),
            "--operation", operation, "--quiet",
            "--fixture-file", str(self.fixture(
                xueqiu_posts() if fixture is None else fixture)),
        ]
        if operation == "search_posts":
            argv += ["--query", "synthetic"]
        if operation == "get_post":
            argv += ["--post-ref", "1"]
        if grant is not None:
            argv += ["--credential-grant", str(grant)]
        return xueqiu_cli.main(argv + list(extra))

    def grant(self, operations=("search_posts",)) -> Path:
        return self.fixture(
            credential_grant(slot_refs=[CREDENTIAL_SLOT_REF],
                             operations=list(operations),
                             target_ref=XUEQIU_TARGET),
            name="grant.json",
        )

    # -- approval ----------------------------------------------------------

    def test_a_proposed_record_is_not_authority(self):
        code = self.run_child(
            governance=self.governance("xueqiu-search-posts", status="proposed"),
            grant=self.grant())
        self.assertEqual(code, 1)
        self.assertIn("owner approval is required",
                      self.summary()["failure_reason"])

    def test_a_record_for_another_operation_is_refused(self):
        code = self.run_child(
            operation="get_post",
            governance=self.governance("xueqiu-search-posts"),
            grant=self.grant(operations=("get_post",)))
        self.assertEqual(code, 1)
        self.assertIn("covers", self.summary()["failure_reason"])

    def test_a_drifted_schema_hash_is_refused_before_anything_is_read(self):
        code = self.run_child(
            governance=self.governance("xueqiu-search-posts",
                                       expected_schema_hash="b" * 64),
            grant=self.grant())
        self.assertEqual(code, 1)
        self.assertIn("does not cover this output contract",
                      self.summary()["failure_reason"])
        self.assertIsNone(self.summary()["artifact"])

    # -- the credential slot ----------------------------------------------

    def test_an_unbound_slot_is_refused_and_the_reason_names_it(self):
        code = self.run_child(grant=None)
        self.assertEqual(code, 1)
        reason = self.summary()["failure_reason"]
        self.assertIn("CrowdCredentialSlotUnbound", reason)
        self.assertIn(CREDENTIAL_SLOT_REF, reason)

    def test_a_grant_for_another_operation_does_not_cover_this_one(self):
        code = self.run_child(grant=self.grant(operations=("get_post",)))
        self.assertEqual(code, 1)
        self.assertIn("does not allow search_posts",
                      self.summary()["failure_reason"])

    def ranking(self) -> dict[str, Any]:
        return {"ranking": [{"symbol": "SH600519", "name": "n", "rank": 1,
                             "value": "1.5"}]}

    def test_the_ranking_needs_no_credential_on_its_fallback_route(self):
        code = self.run_child(
            operation="hot_rank",
            governance=self.governance("xueqiu-hot-rank"),
            fixture=self.ranking(),
            extra=("--fallback-tool", "/nowhere/cn-hk-findata"))
        self.assertEqual(code, 0, self.summary().get("failure_reason"))
        self.assertEqual(self.summary()["observation"]["provenance_label"],
                         "xueqiu_hot_stock_rank_fallback")

    def test_the_ranking_does_need_the_cookie_on_the_primary_route(self):
        # The route decides, not the operation. Configured with a primary tool
        # -- the normal configuration, because the other two operations need
        # one -- the ranking goes through the host's Xueqiu channel, and that
        # needs the cookie like everything else on that channel does.
        code = self.run_child(
            operation="hot_rank",
            governance=self.governance("xueqiu-hot-rank"),
            fixture=self.ranking())
        self.assertEqual(code, 1)
        reason = self.summary()["failure_reason"]
        self.assertIn("CrowdCredentialSlotUnbound", reason)
        self.assertIn(CREDENTIAL_SLOT_REF, reason)

    def test_the_ranking_runs_on_the_primary_route_with_a_grant(self):
        code = self.run_child(
            operation="hot_rank",
            governance=self.governance("xueqiu-hot-rank"),
            grant=self.grant(operations=("hot_rank",)),
            fixture=self.ranking())
        self.assertEqual(code, 0, self.summary().get("failure_reason"))
        self.assertEqual(self.summary()["observation"]["provenance_label"],
                         "xueqiu_agent_reach_channel")

    # -- the artifact ------------------------------------------------------

    def test_what_the_source_said_is_hashed_before_it_is_read(self):
        payload = xueqiu_posts(3)
        self.assertEqual(self.run_child(grant=self.grant(), fixture=payload), 0)
        summary = self.summary()
        expected = hashlib.sha256(self.fixture(payload).read_bytes()).hexdigest()
        self.assertEqual(summary["artifact"]["content_hash"], expected)
        self.assertEqual(summary["observation"]["source_record_refs"],
                         [f"raw-sink:{expected}"])

    def test_the_summary_never_carries_a_credential_value(self):
        self.assertEqual(self.run_child(grant=self.grant()), 0)
        credential = self.summary()["credential"]
        self.assertEqual(set(credential),
                         {"grant_ref", "grant_hash", "credential_slot_refs",
                          "expires_at", "max_calls"})

    # -- the contract ------------------------------------------------------

    def test_a_post_without_an_id_is_refused_rather_than_stored(self):
        code = self.run_child(grant=self.grant(),
                              fixture={"posts": [{"created_at": "2026-09-01"}]})
        self.assertEqual(code, 1)
        self.assertIn("without an id", self.summary()["failure_reason"])

    def test_a_post_without_a_timestamp_is_refused(self):
        code = self.run_child(grant=self.grant(),
                              fixture={"posts": [{"id": "7", "text": "x"}]})
        self.assertEqual(code, 1)
        self.assertIn("without a timestamp", self.summary()["failure_reason"])

    def test_the_observation_matches_the_frozen_contract(self):
        self.assertEqual(self.run_child(grant=self.grant()), 0)
        observation = self.summary()["observation"]
        self.assertEqual(observation["operation"], "search_posts")
        self.assertEqual(len(observation["posts"]), 2)
        self.assertEqual(observation["posts"][0]["post_id"], "1")

    def test_a_failure_still_writes_a_summary_saying_why(self):
        self.run_child(grant=None)
        self.assertTrue((self.out / "summary.json").exists())
        self.assertEqual(self.summary()["status"], "failed")


class XreachChildTests(ChildTestCase):
    def grant(self, operations=("user_timeline",), slots=None) -> Path:
        return self.fixture(
            credential_grant(
                slot_refs=list(slots or CREDENTIAL_SLOT_REFS),
                operations=list(operations), target_ref=XREACH_TARGET),
            name="grant.json")

    def run_child(self, *, operation="user_timeline", governance=None,
                  grant: Path | None = None, fixture=None, extra=()) -> int:
        argv = [
            "--state-dir", str(self.state), "--summary-dir", str(self.out),
            "--governance", str(
                governance or self.governance("x-xreach-user-timeline")),
            "--operation", operation, "--quiet",
            "--fixture-file", str(self.fixture(
                xreach_posts() if fixture is None else fixture)),
        ]
        argv += {"user_timeline": ["--handle", "SyntheticCo"],
                 "search": ["--query", "synthetic"],
                 "thread": ["--post-ref", "100"]}[operation]
        if grant is not None:
            argv += ["--credential-grant", str(grant)]
        return xreach_cli.main(argv + list(extra))

    def test_both_cookie_slots_are_required(self):
        code = self.run_child(grant=self.grant(slots=[CREDENTIAL_SLOT_REFS[0]]))
        self.assertEqual(code, 1)
        reason = self.summary()["failure_reason"]
        self.assertIn(CREDENTIAL_SLOT_REFS[1], reason)

    def test_no_grant_at_all_is_refused_by_name(self):
        self.assertEqual(self.run_child(grant=None), 1)
        self.assertIn("carries no host grant", self.summary()["failure_reason"])

    def test_a_timeline_read_to_its_end_is_enumerated(self):
        self.assertEqual(self.run_child(grant=self.grant()), 0,
                         self.summary().get("failure_reason"))
        self.assertEqual(self.summary()["observation"]["completeness"], "enumerated")

    def test_a_timeline_with_more_pages_is_only_partial(self):
        payload = xreach_posts()
        payload["next_cursor"] = "more"
        self.assertEqual(self.run_child(grant=self.grant(), fixture=payload), 0)
        self.assertEqual(self.summary()["observation"]["completeness"], "partial")

    def test_a_search_is_ranked_whatever_the_cursor_says(self):
        payload = xreach_posts()
        payload["next_cursor"] = None
        code = self.run_child(
            operation="search", governance=self.governance("x-xreach-search"),
            grant=self.grant(operations=("search",)), fixture=payload)
        self.assertEqual(code, 0, self.summary().get("failure_reason"))
        self.assertEqual(self.summary()["observation"]["completeness"], "ranked")

    def test_since_drops_posts_older_than_the_window(self):
        code = self.run_child(grant=self.grant(),
                              extra=("--since", "2026-09-02"))
        self.assertEqual(code, 0, self.summary().get("failure_reason"))
        self.assertEqual(self.summary()["observation"]["posts"][0]["post_id"], "101")

    def test_the_tool_argv_uses_the_tools_own_subcommands(self):
        self.assertEqual(
            xreach_cli.tool_argv("user_timeline", tool="xreach", handle="a",
                                 query=None, post_ref=None, count=20,
                                 cursor=None)[2],
            "tweets",
        )


class EmployeeReviewChildTests(ChildTestCase):
    def run_child(self, *, governance=None, page=None, extra=()) -> int:
        argv = [
            "--state-dir", str(self.state), "--summary-dir", str(self.out),
            "--governance", str(
                governance or self.governance("employee-reviews-blind")),
            "--employer-slug", "SyntheticCo", "--quiet",
            "--fixture-file", str(self.fixture(
                blind_page(unlocked=1, locked=2) if page is None else page,
                name="page.html")),
        ]
        return employee_reviews_cli.main(argv + list(extra))

    def test_a_proposed_record_is_not_authority(self):
        code = self.run_child(
            governance=self.governance("employee-reviews-blind", status="proposed"))
        self.assertEqual(code, 1)
        self.assertIn("owner approval is required",
                      self.summary()["failure_reason"])

    def test_locked_rows_keep_their_ratings_and_lose_their_prose(self):
        self.assertEqual(self.run_child(), 0, self.summary().get("failure_reason"))
        observation = self.summary()["observation"]
        self.assertEqual(observation["body_locked_count"], 2)
        locked = [row for row in observation["reviews"] if row["body_locked"]]
        self.assertEqual(len(locked), 2)
        for row in locked:
            # Every prose field goes, including the one-line summary, which
            # Blind substitutes as readily as the rest.
            self.assertIsNone(row["pros"])
            self.assertIsNone(row["cons"])
            self.assertIsNone(row["summary"])
            # What stays is what is real on a locked row.
            self.assertEqual(row["ratings"]["overall"], "2.0")
            self.assertTrue(row["created_at"])
            self.assertTrue(row["jobgroup"])

    def test_no_placeholder_text_reaches_the_observation(self):
        self.assertEqual(self.run_child(), 0)
        body = json.dumps(self.summary()["observation"], ensure_ascii=False)
        self.assertNotIn("Lorem ipsum", body)

    def test_the_ratings_are_text_because_a_float_is_not_what_was_read(self):
        self.assertEqual(self.run_child(), 0)
        rating = self.summary()["observation"]["reviews"][0]["ratings"]["overall"]
        self.assertIsInstance(rating, str)

    def test_a_page_with_no_reviews_is_a_refusal_not_an_empty_success(self):
        code = self.run_child(page=b"<html><body>nothing here</body></html>")
        self.assertEqual(code, 1)
        self.assertIn("carried no reviews", self.summary()["failure_reason"])

    def test_only_one_host_may_be_reached(self):
        with self.assertRaises(employee_reviews_cli.EmployeeReviewsRunError):
            employee_reviews_cli.fetch_page("https://example.invalid/x",
                                            deadline_seconds=1.0)

    def test_an_employer_slug_cannot_smuggle_a_path(self):
        with self.assertRaises(employee_reviews_cli.EmployeeReviewsRunError):
            employee_reviews_cli.review_page_url("../../etc", 1)

    def test_a_non_ascii_review_survives_the_payload_decode(self):
        """`unicode_escape` decodes through latin-1 and mangles silently.

        An accented name or a CJK location came back as mojibake with nothing
        raised, which is the worst failure a parser has: the review is still
        there, still counted, and no longer says what it said.
        """

        self.assertEqual(self.run_child(
            page=blind_page(unlocked=1, locked=0, accented=True)), 0,
            self.summary().get("failure_reason"))
        location = self.summary()["observation"]["reviews"][0]["location"]
        self.assertEqual(location, "Montréal · 北京")

    def test_rows_past_the_first_page_are_locked_by_position(self):
        """Position is the half that survives Blind changing its filler."""

        self.assertEqual(self.run_child(
            page=blind_page(unlocked=40, locked=0)), 0,
            self.summary().get("failure_reason"))
        reviews = self.summary()["observation"]["reviews"]
        self.assertFalse(reviews[0]["body_locked"])
        self.assertTrue(reviews[30]["body_locked"])
        self.assertIsNone(reviews[30]["pros"])
        self.assertTrue(reviews[30]["ratings"]["overall"])

    def test_the_library_total_travels_with_the_sample(self):
        self.assertEqual(self.run_child(), 0)
        self.assertEqual(self.summary()["observation"]["library_total"], 3)


class GrantEnvelopeTests(ChildTestCase):
    def test_a_missing_grant_file_is_an_unbound_slot(self):
        with self.assertRaises(CrowdCredentialSlotUnbound):
            load_credential_grant(self.root / "absent.json")

    def test_a_malformed_grant_is_refused_rather_than_half_read(self):
        path = self.root / "bad.json"
        path.write_text('{"schema_version": "0.1"}', encoding="utf-8")
        with self.assertRaises(CrowdCredentialSlotUnbound):
            load_credential_grant(path)

    def test_an_expired_grant_does_not_bind_a_slot(self):
        wire = credential_grant(slot_refs=[CREDENTIAL_SLOT_REF],
                                operations=["search_posts"],
                                target_ref=XUEQIU_TARGET,
                                expires_in_hours=-1.0)
        path = write_json(self.root / "expired.json", wire)
        # The envelope itself refuses an expiry before its issuance, which is
        # the same refusal arriving one step earlier.
        with self.assertRaises(CrowdCredentialSlotUnbound):
            require_slots(load_credential_grant(path),
                          slot_refs=[CREDENTIAL_SLOT_REF],
                          operation="search_posts", target_ref=XUEQIU_TARGET)

    def test_a_grant_for_another_target_does_not_bind(self):
        wire = credential_grant(slot_refs=[CREDENTIAL_SLOT_REF],
                                operations=["search_posts"],
                                target_ref="host-tool:somewhere-else")
        grant = load_credential_grant(write_json(self.root / "other.json", wire))
        with self.assertRaises(CrowdCredentialSlotUnbound):
            require_slots(grant, slot_refs=[CREDENTIAL_SLOT_REF],
                          operation="search_posts", target_ref=XUEQIU_TARGET)

    def test_credential_shaped_keys_are_stripped_from_a_tool_response(self):
        cleaned = redacted({"posts": [], "cookie": "x", "auth_token": "y",
                            "Set-Cookie": "z", "CT0": "w"})
        self.assertEqual(set(cleaned), {"posts"})

    def test_a_single_post_keeps_its_author(self):
        """`get_post` returns the post at the top level, and this ran over it.

        The first version matched substrings, and "auth" is a substring of
        "author" and "author_id". A single post came back anonymous with
        nothing raised -- a filter that quietly removes data is worse than no
        filter at all.
        """

        post = {"id": "1", "author": "someone", "author_id": "42",
                "text": "a post", "created_at": "2026-09-01 10:00:00"}
        self.assertEqual(redacted(post), post)


class ArgumentTests(unittest.TestCase):
    """Contradictory arguments end the run before it starts, and say nothing.

    A parser error is not a refusal with a reason: there is no summary to write
    one into, because nothing was asked for coherently. stderr is swallowed so
    that a passing suite stays readable.
    """

    def refuse(self, module: Any, argv: list[str]) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                module.main(argv)

    def test_a_child_must_choose_fixture_or_network(self):
        base = ["--state-dir", "/tmp", "--governance", "/tmp/x"]
        self.refuse(xueqiu_cli, base + ["--operation", "get_post",
                                        "--post-ref", "1"])
        self.refuse(xreach_cli, base + ["--operation", "thread",
                                        "--post-ref", "1"])
        self.refuse(employee_reviews_cli, base + ["--employer-slug", "x"])

    def test_a_search_without_a_query_is_rejected_by_the_parser(self):
        self.refuse(xueqiu_cli, [
            "--state-dir", "/tmp", "--governance", "/tmp/x",
            "--operation", "search_posts", "--fixture-file", "/tmp/f"])

    def test_a_timeline_without_a_handle_is_rejected_by_the_parser(self):
        self.refuse(xreach_cli, [
            "--state-dir", "/tmp", "--governance", "/tmp/x",
            "--operation", "user_timeline", "--fixture-file", "/tmp/f"])
