"""Pure parse reuse never substitutes for the current authority checks."""
import ast
import gzip
import hashlib
import subprocess
import sys
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import pure_source_parse_cache as cache
from dalton_core import public_web_extraction_source as web
from dalton_core import sec_filing_units as units
from dalton_core import statement_context
from dalton_core.store import content_hash
from tests.test_sec_filing_unit_context import XML
from tests.test_public_web_extraction_source import PAGE, remanifest
from tests.test_public_web_fetch_lane import FetchHarness, URL_A, transport_for


class PureParseCacheTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_original_function_bodies_defaults_and_effective_identity_are_exact(self):
        original_sha = '6af256a98dfdfd9f63d54b15259b8a2f29784bd3ba112775faed6fe9b799a91b'
        source = Path(units.__file__).read_bytes()
        original = source.replace(b'from .pure_source_parse_cache import cache_byte_parser\n', b'', 1).replace(
            b"@cache_byte_parser('sec-filing-units-pure-parse:1', byte_arguments=('raw','resource_raw'))\n", b'', 1)
        self.assertEqual(hashlib.sha256(original).hexdigest(), original_sha)
        for module, name, expected in (
            (units, 'parse_filing_units', '82108513c87cb8bd3ecf932b420e8349900144feb8d96c8a9e282385fb401d8e'),
            (web, 'render_public_web_text', 'dd0a297484924035f16c33119bababc81f541312cc2bd3291c77eb1cd485b6c3'),
        ):
            text = Path(module.__file__).read_text()
            node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertEqual(hashlib.sha256(ast.get_source_segment(text, node).encode()).hexdigest(), expected)
            self.assertEqual(inspect.signature(getattr(module, name)), inspect.signature(getattr(module, name).__wrapped__))
        new_sha = hashlib.sha256(source).hexdigest()
        self.assertEqual(statement_context._UNIT_PURE_PARSE_CACHE_SOURCE_ALIAS, {new_sha: original_sha})
        current = statement_context.normalizer_hash()
        read_bytes = Path.read_bytes
        with patch.object(statement_context.Path, 'read_bytes', autospec=True) as read:
            read.side_effect = lambda p: original if str(p) == units.__file__ else read_bytes(p)
            self.assertEqual(statement_context.normalizer_hash(), current)
        changed = source.replace(b'MAX_BYTES=32*1024*1024', b'MAX_BYTES=31*1024*1024')
        self.assertNotEqual(changed, source)
        with patch.object(statement_context.Path, 'read_bytes', autospec=True) as read:
            read.side_effect = lambda p: changed if str(p) == units.__file__ else read_bytes(p)
            self.assertNotEqual(statement_context.normalizer_hash(), current)

    def test_html_and_xml_cold_warm_clear_reload_canonical_and_copy(self):
        for issuer in ('12', '34'):
            raw = XML.replace('>12<', '>' + issuer + '<').encode()
            expected = units.parse_filing_units.__wrapped__(raw)
            cold = units.parse_filing_units(raw)
            warm = units.parse_filing_units(raw)
            self.assertEqual(content_hash(cold), content_hash(expected))
            self.assertEqual(content_hash(warm), content_hash(expected))
            warm['facts'][0]['value'] = 'forged'
            self.assertEqual(units.parse_filing_units(raw), expected)
        expected_web = web.render_public_web_text.__wrapped__(PAGE, raw_media_type='text/html')
        self.assertEqual(web.render_public_web_text(PAGE, raw_media_type='text/html'), expected_web)
        warm = web.render_public_web_text(PAGE, raw_media_type='text/html')
        warm['text'] = 'forged'
        self.assertEqual(web.render_public_web_text(PAGE, raw_media_type='text/html'), expected_web)
        self.assertGreater(cache.cache_info()['hits'], 0)
        cache.clear()
        self.assertEqual(units.parse_filing_units(raw), expected)
        # A real module reload is isolated so imported exception classes in
        # other test fixtures retain their normal Python identity.
        code = """import importlib,sys
from dalton_core import sec_filing_units as u, pure_source_parse_cache as c
from dalton_core.store import content_hash
raw=sys.argv[1].encode();first=u.parse_filing_units(raw);before=c.cache_info()['misses']
importlib.reload(u)
assert content_hash(u.parse_filing_units(raw))==content_hash(first)
assert c.cache_info()['misses']==before+1
"""
        run = subprocess.run([sys.executable, '-c', code, raw.decode()], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_bytes_resource_rule_encoding_limits_and_renderer_options_are_in_key(self):
        first = units.parse_filing_units(XML.encode())
        other = units.parse_filing_units(XML.replace('1,234.5', '1,234.6').encode())
        self.assertNotEqual(content_hash(first), content_hash(other))
        for options in ({'rule_ref': units.NUMERIC_RULE_REF}, {'resource_raw': XML.encode()}):
            self.assertEqual(units.parse_filing_units(XML.encode(), **options), units.parse_filing_units.__wrapped__(XML.encode(), **options))
        for options in ({'legacy': True}, {'max_source_chars': 20}, {'raw_media_type': 'text/html; charset=utf-8'}):
            kwargs = {'raw_media_type': 'text/html', **options}
            self.assertEqual(web.render_public_web_text(PAGE, **kwargs), web.render_public_web_text.__wrapped__(PAGE, **kwargs))
        self.assertEqual(web.render_public_web_text(gzip.compress(PAGE), raw_media_type='text/html')['text'],
                         web.render_public_web_text(PAGE, raw_media_type='text/html')['text'])

    def test_failures_remain_uncached_with_original_error_type_and_text(self):
        for function, raw, options in (
            (units.parse_filing_units, b'broken', {}),
            (units.parse_filing_units, XML.encode(), {'rule_ref': 'unknown'}),
            (web.render_public_web_text, b'\xff', {'raw_media_type': 'text/html'}),
            (web.render_public_web_text, PAGE, {'raw_media_type': 'text/html', 'max_source_chars': 0}),
        ):
            with self.subTest(options=options):
                try: function.__wrapped__(raw, **options)
                except Exception as exc: expected = (type(exc), str(exc))
                else: self.fail('fixture must fail')
                before = cache.cache_info()['entries']
                for _ in range(2):
                    try: function(raw, **options)
                    except Exception as exc: self.assertEqual((type(exc), str(exc)), expected)
                    else: self.fail('cache must not accept failure')
                self.assertEqual(cache.cache_info()['entries'], before)
        with self.assertRaises(TypeError) as wrapped: units.parse_filing_units()
        with self.assertRaises(TypeError) as original: units.parse_filing_units.__wrapped__()
        self.assertEqual(str(wrapped.exception), str(original.exception))

    def test_bounds_bypass_and_lru_preserve_result_aliases_and_types(self):
        calls = []
        @cache.cache_byte_parser('test-tree')
        def parse(raw):
            calls.append(raw)
            shared = ['x' * len(raw)]
            return {'a': shared, 'b': shared, 'tuple': (True, None, 3)}
        with patch.multiple(cache, MAX_ENTRIES=2, MAX_INPUT_BYTES=8, MAX_VALUE_BYTES=300, MAX_TOTAL_BYTES=120):
            first = parse(b'a'); second = parse(b'a')
            self.assertIs(second['a'], second['b'])
            self.assertIsNot(first['a'], second['a'])
            self.assertIsInstance(second['tuple'], tuple)
            for raw in (b'bb', b'ccc', b'dddd'): parse(raw)
            self.assertLessEqual(cache.cache_info()['entries'], 2)
            self.assertLessEqual(cache.cache_info()['serialized_bytes'], 120)
            before = len(calls)
            for _ in range(2): parse(b'input-over-limit')
            self.assertEqual(len(calls), before + 2)
        cache.clear()
        with patch.object(cache, 'MAX_VALUE_BYTES', 1):
            parse(b'a'); parse(b'a')
            self.assertEqual(cache.cache_info()['entries'], 0)

    def test_source_contract_changes_miss_and_non_html_is_not_cached(self):
        web.render_public_web_text(PAGE, raw_media_type='text/html')
        before = cache.cache_info()['misses']
        original_read = Path.read_bytes
        with patch.object(Path, 'read_bytes', lambda p: original_read(p) + b'\n# changed parser contract'):
            self.assertEqual(web.render_public_web_text(PAGE, raw_media_type='text/html'), web.render_public_web_text.__wrapped__(PAGE, raw_media_type='text/html'))
        self.assertEqual(cache.cache_info()['misses'], before + 1)
        before = cache.cache_info()
        for _ in range(2): web.render_public_web_text(b'text', raw_media_type='text/plain')
        self.assertEqual(cache.cache_info(), before)

    def test_warm_normal_authority_path_rejects_spool_manifest_and_withdrawn_profile(self):
        with tempfile.TemporaryDirectory() as td:
            h = FetchHarness(Path(td), transport=transport_for(PAGE))
            try:
                receipt = h.discover()
                fetched = h.fetch.fetch(h.fetch.build_request(h.authority(receipt, URL_A)))
                manifest = h.fetch.manifest(fetched)
                def verify(**kwargs):
                    return web.verified_public_web_source(h.core, kwargs.get('spool', h.spool), kwargs.get('manifest', manifest), h.fetch.receipts)
                expected = verify(); self.assertEqual(verify(), expected)
                self.assertGreater(cache.cache_info()['hits'], 0)
                class ChangedSpool:
                    def read_object(self, _sha): return PAGE.replace(b'CFO', b'CEO')
                with self.assertRaises(web.PublicWebSourceConflict): verify(spool=ChangedSpool())
                with self.assertRaises(web.PublicWebSourceConflict):
                    verify(manifest=remanifest(manifest, connector_profile_hash='0' * 64))
                with patch.object(type(h.fetch.receipts), 'get_profile', return_value=None):
                    with self.assertRaises(web.PublicWebSourceConflict): verify()
                self.assertEqual(verify(), expected)
            finally: h.close()
