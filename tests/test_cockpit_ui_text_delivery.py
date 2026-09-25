"""Reviewed UI translations reach the Cockpit per view, by key, in bounded pages.

Live on 2026-09-24 the reviewed mapping held 8,124 strings (4.46 MB of JSON).
The overview embedded the whole mapping and dropped it whole past 2 MB, so the
page showed not one reviewed translation.  The page now asks
``/v1/cockpit/ui-texts`` for the strings each response it reads holds.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path

from dalton_core import agenda_control
from dalton_core import research_localization_store as store
from dalton_core.cockpit_plane import CockpitPlane
from dalton_core.research_localization import build_localization
from dalton_core.research_localization_store import (
    load_ui_texts, lookup_ui_texts, publish_ui_texts, ui_text_key, ui_texts_revision)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "src" / "dalton_core" / "cockpit_control.html"
VERIFIER = {"verdict": "pass", "faithful": True, "no_new_facts": True,
            "meaning_preserved": True, "findings": []}
CHECKING = "正文正在检查文字表达，完成后会显示。"


def batch(body: str, translated: str) -> dict:
    source = {"kind": "initial_screen", "version_ref": "ui:" + body, "status": "available",
              "content_hash": "a" * 64, "approval": {"status": "not_applicable"},
              "sections": [{"title": "Text", "body": body, "sources": [], "numbers": []}]}
    localization = build_localization(source, {"sections": [
        {"index": 0, "title": "界面文字", "body": translated, "gaps": []}]}, VERIFIER)
    return {"source": source, "localization": localization}


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "core.sqlite"
        self.directory = self.root / "research-localization"

    def tearDown(self) -> None:
        self.temp.cleanup()


class UiTextLookupTests(_Store):
    def test_key_is_the_sha256_prefix_of_the_exact_utf8_string(self) -> None:
        self.assertEqual(ui_text_key("收入增长。"),
                         hashlib.sha256("收入增长。".encode()).hexdigest()[:16])
        self.assertIsNone(ui_text_key("\ud800"))

    def test_only_requested_reviewed_keys_are_served(self) -> None:
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。"),
                                          batch("Revenue fell.", "收入下降。")])
        wanted, unreviewed = ui_text_key("Revenue increased."), ui_text_key("Not reviewed.")
        page = lookup_ui_texts(self.db, [wanted, unreviewed, "not-a-key", wanted.upper()])
        self.assertEqual(page["texts"], {wanted: "收入增长。"})
        self.assertEqual(page["same"], [])
        self.assertEqual(page["deferred"], [])
        self.assertEqual(page["withheld"], [])
        self.assertEqual(page["revision"], ui_texts_revision(self.db))

    def test_reviewed_identity_is_listed_without_repeating_the_string(self) -> None:
        with unittest.mock.patch.object(store, "_load_ui_cached", return_value={
                "Same.": "Same.", "Other.": "其他。"}):
            self.directory.mkdir()
            (self.directory / "ui-texts.json").write_text("{}")
            page = lookup_ui_texts(self.db, [ui_text_key("Same."), ui_text_key("Other.")])
        self.assertEqual(page["same"], [ui_text_key("Same.")])
        self.assertEqual(page["texts"], {ui_text_key("Other."): "其他。"})

    def test_a_page_over_the_bound_is_cut_and_the_rest_deferred_never_dropped(self) -> None:
        mapping = {f"Line {n} " + "x" * 200: f"第 {n} 行" + "译" * 300 for n in range(60)}
        with unittest.mock.patch.object(store, "_load_ui_cached", return_value=mapping):
            self.directory.mkdir()
            (self.directory / "ui-texts.json").write_text("{}")
            pending = [ui_text_key(k) for k in mapping] + [ui_text_key("absent")]
            received, pages = {}, 0
            while pending:
                page = lookup_ui_texts(self.db, pending, max_bytes=8 * 1024)
                pages += 1
                body = json.dumps(page["texts"], ensure_ascii=False).encode()
                self.assertLessEqual(len(body), 8 * 1024)
                self.assertTrue(page["texts"])
                received.update(page["texts"])
                self.assertNotIn(ui_text_key("absent"), page["deferred"])
                pending = page["deferred"]
        self.assertGreater(pages, 5)
        self.assertEqual(received, {ui_text_key(k): v for k, v in mapping.items()})

    def test_keys_past_one_page_are_deferred_and_one_oversize_text_is_withheld(self) -> None:
        mapping = {f"k{n}": f"译{n}" for n in range(10)}
        mapping["huge"] = "长" * 5000
        with unittest.mock.patch.object(store, "_load_ui_cached", return_value=mapping):
            self.directory.mkdir()
            (self.directory / "ui-texts.json").write_text("{}")
            keys = [ui_text_key("huge")] + [ui_text_key(f"k{n}") for n in range(10)]
            page = lookup_ui_texts(self.db, keys, max_keys=4, max_bytes=1024)
        self.assertEqual(page["withheld"], [ui_text_key("huge")])
        self.assertEqual(len(page["texts"]), 3)
        self.assertEqual(page["deferred"], keys[4:])

    def test_a_mapping_far_past_the_old_2mb_cap_still_serves_each_view(self) -> None:
        mapping = {f"Approved sentence {n}. " + "y" * 150: f"已审核译文 {n}。" + "文" * 150
                   for n in range(8000)}
        self.assertGreater(len(json.dumps(mapping, ensure_ascii=False).encode()), 4_000_000)
        with unittest.mock.patch.object(store, "_load_ui_cached", return_value=mapping):
            self.directory.mkdir()
            (self.directory / "ui-texts.json").write_text("{}")
            view = list(mapping)[:150]
            page = lookup_ui_texts(self.db, [ui_text_key(k) for k in view])
        self.assertEqual(len(page["texts"]), 150)
        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()),
                        store.UI_TEXT_MAX_RESPONSE_BYTES + 1024)

    def test_requests_reuse_one_index_until_a_publish_moves_the_revision(self) -> None:
        from dalton_core import research_localization
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。")])
        first = ui_texts_revision(self.db)  # builds the index once
        with unittest.mock.patch.object(store, "validate_localization",
                                        wraps=research_localization.validate_localization) as validate:
            for _ in range(5):
                lookup_ui_texts(self.db, [ui_text_key("Revenue increased.")])
                load_ui_texts(self.db)
            self.assertEqual(validate.call_count, 0)
            publish_ui_texts(self.directory, [batch("Revenue fell.", "收入下降。")])
            validate.reset_mock()
            page = lookup_ui_texts(self.db, [ui_text_key("Revenue fell.")])
            lookup_ui_texts(self.db, [ui_text_key("Revenue fell.")])
        self.assertNotEqual(ui_texts_revision(self.db), first)
        self.assertEqual(page["texts"], {ui_text_key("Revenue fell."): "收入下降。"})
        # Rebuilt once for the new revision (the published batch plus the one
        # validated during publish), not once per request.
        self.assertLessEqual(validate.call_count, 3)

    def test_a_publish_revalidates_only_the_records_it_rewrote(self) -> None:
        # 2026-09-25: every publish (live, every one to two minutes) made the
        # next reader re-read and re-validate all ~860 records: 5-8 s of
        # regular-expression work under the GIL per rebuild.
        from dalton_core import research_localization
        publish_ui_texts(self.directory, [batch(f"Line {n}.", f"第 {n} 行。") for n in range(20)])
        ui_texts_revision(self.db)
        with unittest.mock.patch.object(store, "validate_localization",
                                        wraps=research_localization.validate_localization) as validate:
            publish_ui_texts(self.directory, [batch("Line 20.", "第 20 行。")])
            validate.reset_mock()
            page = lookup_ui_texts(self.db, [ui_text_key(f"Line {n}.") for n in range(21)])
        self.assertEqual(len(page["texts"]), 21)
        self.assertEqual(validate.call_count, 1)

    def test_a_republished_batch_is_read_again_and_a_removed_one_is_dropped(self) -> None:
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。"),
                                          batch("Revenue fell.", "收入下降。")])
        self.assertEqual(lookup_ui_texts(self.db, [ui_text_key("Revenue increased.")])["texts"],
                         {ui_text_key("Revenue increased."): "收入增长。"})
        # The same batch published again with a corrected translation replaces
        # its record; the memo is keyed by the record's file state.
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入有所增长。")])
        self.assertEqual(lookup_ui_texts(self.db, [ui_text_key("Revenue increased.")])["texts"],
                         {ui_text_key("Revenue increased."): "收入有所增长。"})
        # A record that becomes unreadable costs its own strings only.
        index = json.loads((self.directory / "ui-texts.json").read_text())
        fell = [ref for ref in index["batch_refs"]
                if "Revenue fell." in (self.directory / "ui-records" / f"{ref}.json").read_text()]
        (self.directory / "ui-records" / f"{fell[0]}.json").write_text("{broken")
        publish_ui_texts(self.directory, [batch("Margin rose.", "利润率上升。")])
        page = lookup_ui_texts(self.db, [ui_text_key(t) for t in
                                         ("Revenue increased.", "Revenue fell.", "Margin rose.")])
        self.assertEqual(page["texts"], {ui_text_key("Revenue increased."): "收入有所增长。",
                                         ui_text_key("Margin rose."): "利润率上升。"})

    def test_concurrent_readers_after_a_publish_share_one_rebuild(self) -> None:
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。")])
        ui_texts_revision(self.db)
        publish_ui_texts(self.directory, [batch("Revenue fell.", "收入下降。")])
        barrier = threading.Barrier(8)
        pages = []

        def read() -> None:
            barrier.wait(5)
            pages.append(lookup_ui_texts(self.db, [ui_text_key("Revenue fell.")]))

        with unittest.mock.patch.object(store, "_load_ui_cached",
                                        wraps=store._load_ui_cached) as load:
            threads = [threading.Thread(target=read) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(pages), 8)
        self.assertTrue(all(p["texts"] == {ui_text_key("Revenue fell."): "收入下降。"} for p in pages))
        self.assertEqual(load.call_count, 1)

    def test_no_mapping_serves_nothing_and_has_no_revision(self) -> None:
        self.assertIsNone(ui_texts_revision(self.db))
        page = lookup_ui_texts(self.db, [ui_text_key("Anything.")])
        self.assertEqual((page["revision"], page["texts"], page["same"]), (None, {}, []))


class OverviewCarriesRevisionOnlyTests(unittest.TestCase):
    def test_overview_source_no_longer_embeds_or_caps_the_mapping(self) -> None:
        source = (ROOT / "src" / "dalton_core" / "cockpit_plane.py").read_text(encoding="utf-8")
        self.assertNotIn('"text_localizations"', source)
        self.assertNotIn("2_000_000", source)
        self.assertIn('"text_localization_revision": text_localization_revision', source)


def _javascript_block() -> str:
    source = HTML.read_text(encoding="utf-8")
    start = source.index("const UI_TEXT_ENDPOINT=")
    end = source.index("\n", source.index("const uiTextPath="))
    return source[start:end]


class _Plane:
    def __init__(self, db: Path) -> None:
        self.config = types.SimpleNamespace(core_db=db)

    ui_texts = CockpitPlane.ui_texts


class CockpitUiTextDeliveryEndToEndTests(_Store):
    """The page's own resolver against the real HTTP handler and store."""

    login = "owner@example"

    def setUp(self) -> None:
        super().setUp()
        if shutil.which("node") is None:
            self.skipTest("node is not installed")
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(tailscale_host="cockpit.invalid"),
            allowed_login=lambda value: value if value == self.login else None,
            session_cookie_name="dalton_test", _sessions={}, _lock=threading.Lock(),
            cockpit_plane=_Plane(self.db))
        app.session = types.MethodType(agenda_control.AgendaControlApplication.session, app)
        app.cockpit_view = types.MethodType(
            agenda_control.AgendaControlApplication.cockpit_view, app)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agenda_control._handler(app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def run_page(self, script: str) -> dict:
        program = "\n".join([
            "let UI_TEXT=Object.create(null);",
            "let UI_TEXT_REVISION=0;",
            "let FINAL_RESEARCH_REQUIRED=true;",
            "const displayText=value=>typeof value===\"string\"&&Object.prototype.hasOwnProperty.call(UI_TEXT,value)?UI_TEXT[value]:value;",
            "const finalResearchText=value=>{if(typeof value!==\"string\"||!value)return value;"
            "return FINAL_RESEARCH_REQUIRED&&!Object.prototype.hasOwnProperty.call(UI_TEXT,value)"
            "?\"正文正在检查文字表达，完成后会显示。\":displayText(value)};",
            "const realFetch=globalThis.fetch;const requests=[];",
            f"globalThis.fetch=(path,options={{}})=>{{requests.push(path);return realFetch({json.dumps(self.base)}+path,"
            f"{{...options,headers:{{...(options.headers||{{}}),'Tailscale-User-Login':{json.dumps(self.login)}}}}})}};",
            _javascript_block(),
            "(async()=>{", script, "})().catch(e=>{console.error(e);process.exit(1)});",
        ])
        result = subprocess.run(["node", "-e", program], text=True, capture_output=True,
                                timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_page_key_matches_server_key(self) -> None:
        samples = ["", "a", "收入增长。", "x" * 55, "y" * 56, "z" * 64, "emoji 😀 中文\n\t\"q\"",
                   "é" * 300]
        keys = self.run_page(
            f"console.log(JSON.stringify({json.dumps(samples)}.map(uiTextKey)))")
        self.assertEqual(keys, [ui_text_key(text) for text in samples])

    def test_view_payload_gets_reviewed_text_and_unreviewed_stays_in_review(self) -> None:
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。")])
        payload = {"text_localization_revision": ui_texts_revision(self.db),
                   "companies": [{"note": "Revenue increased.", "gap": "Not reviewed yet."}],
                   "csrf_token": "token"}
        result = self.run_page(f"""
            await resolveUiTexts({json.dumps(payload)});
            const first=requests.length;
            await resolveUiTexts({json.dumps(payload)});
            console.log(JSON.stringify({{
              reviewed: finalResearchText("Revenue increased."),
              unreviewed: finalResearchText("Not reviewed yet."),
              revision: UI_TEXT_REVISION, first, second: requests.length - first,
              asked: requests[0]}}));""")
        self.assertEqual(result["reviewed"], "收入增长。")
        self.assertEqual(result["unreviewed"], CHECKING)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["first"], 1)
        self.assertEqual(result["second"], 0, "a resolved string is not asked for again")
        self.assertTrue(result["asked"].startswith("/v1/cockpit/ui-texts?keys="))
        self.assertNotIn("token", result["asked"])
        self.assertNotIn(ui_text_key("token"), result["asked"])

    def test_bounded_pages_are_followed_until_every_reviewed_string_arrives(self) -> None:
        texts = {f"Line {n} " + "x" * 80: f"第 {n} 行" + "译" * 200 for n in range(40)}
        with unittest.mock.patch.object(store, "_load_ui_cached", return_value=texts), \
                unittest.mock.patch.object(store, "UI_TEXT_MAX_RESPONSE_BYTES", 4096), \
                unittest.mock.patch.object(store, "UI_TEXT_MAX_KEYS_PER_REQUEST", 25):
            self.directory.mkdir()
            (self.directory / "ui-texts.json").write_text("{}")
            payload = {"rows": [{"text": source} for source in texts] + [{"text": "absent"}]}
            result = self.run_page(f"""
                await resolveUiTexts({json.dumps(payload, ensure_ascii=False)});
                console.log(JSON.stringify({{shown: Object.keys(UI_TEXT).length,
                  requests: requests.length, absent: finalResearchText("absent")}}));""")
        self.assertEqual(result["shown"], 40)
        self.assertGreater(result["requests"], 5)
        self.assertEqual(result["absent"], CHECKING)

    def test_a_new_revision_re_asks_strings_that_were_not_reviewed_before(self) -> None:
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。")])
        before = ui_texts_revision(self.db)
        publish_ui_texts(self.directory, [batch("Margin rose.", "利润率上升。")])
        after = ui_texts_revision(self.db)
        # The page first saw "Margin rose." before it was reviewed.
        stale = {"text_localization_revision": before, "a": "Margin rose."}
        fresh = {"text_localization_revision": after, "a": "Margin rose."}
        result = self.run_page(f"""
            UI_TEXT_SOURCE={json.dumps(before)};
            UI_TEXT_RESOLVED.add(uiTextKey("Margin rose."));
            await resolveUiTexts({json.dumps(stale)});
            const stillChecking=finalResearchText("Margin rose.");
            await resolveUiTexts({json.dumps(fresh)});
            console.log(JSON.stringify({{stillChecking, now: finalResearchText("Margin rose.")}}));""")
        self.assertEqual(result["stillChecking"], CHECKING)
        self.assertEqual(result["now"], "利润率上升。")

    def test_endpoint_keeps_identity_gate_and_security_headers(self) -> None:
        import urllib.error
        import urllib.request
        publish_ui_texts(self.directory, [batch("Revenue increased.", "收入增长。")])
        url = f"{self.base}/v1/cockpit/ui-texts?keys={ui_text_key('Revenue increased.')}"
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(url, timeout=10)  # noqa: S310
        self.assertEqual(refused.exception.code, 403)
        request = urllib.request.Request(url, headers={"Tailscale-User-Login": self.login})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = json.loads(response.read())
            headers = response.headers
        self.assertEqual(body["texts"], {ui_text_key("Revenue increased."): "收入增长。"})
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")


class CockpitPageWiringTests(unittest.TestCase):
    def test_every_view_read_resolves_its_strings_before_rendering(self) -> None:
        source = HTML.read_text(encoding="utf-8")
        self.assertEqual(source.count("if(uiTextPath(path))await resolveUiTexts(v).catch(()=>{});"), 2)
        self.assertNotIn("o.text_localizations", source)
        self.assertNotIn("UI_TEXT=nextUiText", source)
        # Translations are written as text, never as markup.
        block = _javascript_block()
        self.assertNotIn("innerHTML", block)
        self.assertNotRegex(block, re.compile(r"\beval\("))


if __name__ == "__main__":
    unittest.main()
