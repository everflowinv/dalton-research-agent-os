from __future__ import annotations

import copy
import hashlib
import json
import unittest

from dalton_core.research_localization import (
    ResearchLocalizationError,
    build_localization,
    build_prompt,
    build_verifier_prompt,
    select_localized,
    validate_localized_text,
    validate_localization,
)


def product():
    return {
        "kind": "dossier", "version_ref": "dossier-version:1", "status": "available",
        "approval": {"status": "pending_human_decision"},
        "sections": [{
            "title": "Demand", "body": "Revenue was USD 1,234.50, up 5.2%.",
            "gaps": ["Missing FY2027 margin"],
            "sources": [{"ref": "claim-version:" + "1" * 64}],
            "numbers": [{"text": "USD 1,234.50", "period": "FY2026"}],
        }],
    }


def localized():
    return {"sections": [{"index": 0, "title": "需求",
                           "body": "收入为 USD 1,234.50，同比增长 5.2%。",
                           "gaps": ["缺少 FY2027 利润率"]}]}


def verifier():
    return {"verdict": "pass", "faithful": True, "no_new_facts": True,
            "meaning_preserved": True, "findings": []}


class ResearchLocalizationTests(unittest.TestCase):
 def test_localization_replays_and_only_overlays_display_prose(self):
    source = product()
    candidate = build_localization(source, localized(), verifier())
    self.assertEqual(validate_localization(source, candidate), candidate)
    selected = select_localized(source, candidate)
    self.assertEqual(selected["sections"][0]["title"], "需求")
    self.assertTrue(selected["sections"][0]["body"].startswith("收入为"))
    self.assertEqual(selected["sections"][0]["sources"], source["sections"][0]["sources"])
    self.assertEqual(selected["sections"][0]["numbers"], source["sections"][0]["numbers"])
    self.assertEqual(selected["status"], source["status"])
    self.assertEqual(selected["approval"], source["approval"])
    self.assertNotIn("localization", source)


 def test_known_legacy_rules_replay_but_unknown_rules_are_rejected(self):
    source = product()
    current = build_localization(source, localized(), verifier())
    legacy = copy.deepcopy(current); legacy["rules_version"] = "simplified-chinese-research-prose:0.2"
    body = dict(legacy); body.pop("content_hash")
    legacy["content_hash"] = hashlib.sha256((json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    self.assertEqual(validate_localization(source, legacy), legacy)
    unknown = copy.deepcopy(legacy); unknown["rules_version"] = "simplified-chinese-research-prose:0.1"
    body = dict(unknown); body.pop("content_hash")
    unknown["content_hash"] = hashlib.sha256((json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
    with self.assertRaisesRegex(ResearchLocalizationError, "identity is unsupported"):
        validate_localization(source, unknown)
    self.assertEqual(current["rules_version"], "simplified-chinese-research-prose:0.3")

 def test_localization_rejects_source_drift_added_number_and_unclean_verifier(self):
    source = product()
    candidate = build_localization(source, localized(), verifier())
    changed = copy.deepcopy(source); changed["sections"][0]["body"] += " Changed."
    with self.assertRaisesRegex(ResearchLocalizationError, "source identity"):
        validate_localization(changed, candidate)
    added = localized(); added["sections"][0]["body"] += " 目标 6.0%。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        build_localization(source, added, verifier())
    failed = verifier(); failed["verdict"] = "reject"; failed["findings"] = ["删掉缺口"]
    with self.assertRaisesRegex(ResearchLocalizationError, "did not pass"):
        build_localization(source, localized(), failed)


 def test_prompts_require_chinese_fidelity_without_granting_new_research(self):
    producer = build_prompt(product())
    review = build_verifier_prompt(product(), localized())
    self.assertIn("fluent Simplified Chinese", producer)
    self.assertIn("not new research", producer)
    self.assertIn("Preserve every authoritative value", producer)
    self.assertIn("no_new_facts", review)
    self.assertIn("meaning_preserved", review)

 def test_preflight_sees_numbers_adjacent_to_chinese_and_ignores_opaque_refs(self):
    source = product()
    source["sections"][0]["body"] = (
        "2026年收入12.5亿元，来源 claim-version:" + "2" * 64)
    translated = {"sections": [{"index": 0, "title": "收入",
                                  "body": "收入在2026年为12.5亿元。",
                                  "gaps": ["Missing FY2027 margin"]}]}
    checked = validate_localized_text(source, translated)
    self.assertEqual(checked[0]["index"], 0)
    translated["sections"][0]["body"] = "收入在2026年为13.5亿元。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_english_number_words_and_months_may_be_rendered_as_chinese_digits(self):
    source = product()
    source["sections"][0]["body"] = (
        "GIS book-to-bill was below one; management discussed it in December.")
    translated = {"sections": [{"index": 0, "title": "订单趋势",
                                  "body": "GIS 订单出货比低于 1；管理层在 12 月讨论了这一点。",
                                  "gaps": ["缺少 FY2027 利润率"]}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    translated["sections"][0]["body"] += " 目标为 13。"
    with self.assertRaises(ResearchLocalizationError) as caught:
        validate_localized_text(source, translated)
    self.assertIn('"section_index": 0', str(caught.exception))
    self.assertIn('"added_tokens": ["13"]', str(caught.exception))

 def test_preflight_reports_missing_and_added_tokens_by_section(self):
    source = product()
    bad = localized()
    bad["sections"][0]["body"] = "收入为 USD 1,235.50，同比增长 6.2%。"
    with self.assertRaises(ResearchLocalizationError) as caught:
        validate_localized_text(source, bad)
    message = str(caught.exception)
    self.assertIn('"section_index": 0', message)
    self.assertIn('"missing_tokens": ["1234.50", "5.2%"]', message)
    self.assertIn('"added_tokens": ["1235.50", "6.2%"]', message)

 def test_preflight_allows_only_unit_bound_deterministic_display_rounding(self):
    source = product()
    source["sections"][0]["body"] = "Revenue was 15623445 USD and margin was 5.24%."
    translated = {"sections": [{"index": 0, "title": "财务摘要",
                                  "body": "收入为 1562 万美元，利润率为 5.2%。",
                                  "gaps": ["缺少 FY2027 利润率"]}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    translated["sections"][0]["body"] = "收入为 1563 万美元，利润率为 5.2%。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_preflight_handles_iso_dates_grouping_and_large_amount_precision(self):
    source = product()
    source["sections"][0]["body"] = (
        "On 2026-06-30 revenue was USD 18044066000 and margin was 1.12%.")
    for amount in ("180.44", "180.4"):
        translated = {"sections": [{"index": 0, "title": "财务摘要",
            "body": f"2026-06-30收入为{amount}亿美元，利润率为1.1%。",
            "gaps": ["缺少 FY2027 利润率"]}]}
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    grouped = {"sections": [{"index": 0, "title": "财务摘要",
        "body": "2026-06-30收入为18,044,066,000美元（原值18,044,066,000美元），利润率为1.12%。",
        "gaps": ["缺少 FY2027 利润率"]}]}
    self.assertEqual(validate_localized_text(source, grouped)[0]["index"], 0)

 def test_complete_calendar_quarter_ranges_may_use_exact_chinese_quarter_names(self):
    source = product()
    source["sections"][0].update(body=(
        "2026-04-01\u81f32026-06-30 revenue was USD 17162000000, up 1.09%; "
        "2026-01-01\u81f32026-03-31 revenue was USD 15917000000, up 9.46%; "
        "2025-04-01\u81f32025-06-30 revenue was USD 16977000000, up 7.65%; "
        "2024-07-01\u81f32024-09-30 revenue was USD 14968000000, up 1.46%."), gaps=[])
    translated = {"sections": [{"index": 0, "title": "\u6536\u5165\u8d8b\u52bf", "body": (
        "2026\u5e74\u7b2c\u4e8c\u5b63\u5ea6\u6536\u5165\u4e3a171.62\u4ebf\u7f8e\u5143\uff0c\u540c\u6bd4\u589e\u957f1.1%\uff1b"
        "2026\u5e74\u7b2c\u4e00\u5b63\u5ea6\u6536\u5165\u4e3a159.17\u4ebf\u7f8e\u5143\uff0c\u540c\u6bd4\u589e\u957f9.5%\uff1b"
        "2025\u5e74\u7b2c\u4e8c\u5b63\u5ea6\u6536\u5165\u4e3a169.77\u4ebf\u7f8e\u5143\uff0c\u540c\u6bd4\u589e\u957f7.7%\uff1b"
        "2024\u5e74\u7b2c\u4e09\u5b63\u5ea6\u6536\u5165\u4e3a149.68\u4ebf\u7f8e\u5143\uff0c\u540c\u6bd4\u589e\u957f1.5%\u3002"), "gaps": []}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)

 def test_calendar_quarter_alias_rejects_changed_or_nonstandard_boundaries(self):
    cases = (
        ("2026-04-01\u81f32026-06-30", "2026\u5e74\u7b2c\u4e09\u5b63\u5ea6"),
        ("2026-04-01\u81f32026-06-30", "2025\u5e74\u7b2c\u4e8c\u5b63\u5ea6"),
        ("2026-04-02\u81f32026-06-30", "2026\u5e74\u7b2c\u4e8c\u5b63\u5ea6"),
        ("2026-01-01\u81f32026-06-30", "2026\u5e74\u7b2c\u4e00\u5b63\u5ea6"),
    )
    for original, body in cases:
        with self.subTest(original=original, body=body):
            source = product(); source["sections"][0].update(body=original, gaps=[])
            translated = {"sections": [{"index": 0, "title": "\u671f\u95f4", "body": body, "gaps": []}]}
            with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
                validate_localized_text(source, translated)

 def test_preflight_never_discards_real_negative_signs(self):
    for source_number, changed in (("-12", "12"), ("-10.5", "10.5")):
        source = product()
        source["sections"][0]["body"] = f"Operating result was {source_number} USD."
        translated = {"sections": [{"index": 0, "title": "经营结果",
            "body": f"经营结果为{changed}美元。",
            "gaps": ["缺少 FY2027 利润率"]}]}
        with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
            validate_localized_text(source, translated)

 def test_preflight_accepts_explicit_dollar_magnitudes_and_grouped_usd(self):
    cases = (
        ("Revenue was $1.353 billion.", "收入为13.53亿美元。"),
        ("Revenue was $970 million.", "收入为9.7亿美元。"),
        ("Revenue was 1,562,000 USD.", "收入为156.2万美元。"),
    )
    for original, body in cases:
        source = product(); source["sections"][0]["body"] = original
        translated = {"sections": [{"index": 0, "title": "收入", "body": body,
            "gaps": ["缺少 FY2027 利润率"]}]}
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    source["sections"][0]["body"] = "Revenue was $970 million."
    translated["sections"][0]["body"] = "收入为9.8亿美元。"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_preflight_accepts_only_explicit_fiscal_year_and_cheng_equivalents(self):
    cases = (
        ("FY26 revenue outlook", "2026财年收入展望"),
        ("FY26 revenue outlook", "2026财年收入展望：2026财年需求疲软"),
        ("fiscal 2027 outlook", "2027财年展望（FY27）"),
        ("约两成五的收入", "约25%的收入"),
    )
    for original, body in cases:
        source = product(); source["sections"][0].update(body=original, gaps=[])
        translated = {"sections": [{"index": 0, "title": "展望", "body": body, "gaps": []}]}
        self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    source["sections"][0]["body"] = "约两成五的收入"
    translated["sections"][0]["body"] = "约26%的收入"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

    source["sections"][0]["body"] = "FY26 revenue outlook"
    translated["sections"][0]["body"] = "2026财年收入展望：2027财年需求疲软"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_preflight_allows_same_year_date_range_to_omit_repeated_year(self):
    source = product(); source["sections"][0].update(
        body="Period 2026-01-01..2026-06-30", gaps=[])
    translated = {"sections": [{"index": 0, "title": "期间",
        "body": "2026年1月1日至6月30日", "gaps": []}]}
    self.assertEqual(validate_localized_text(source, translated)[0]["index"], 0)
    translated["sections"][0]["body"] = "2026年1月1日至7月30日"
    with self.assertRaisesRegex(ResearchLocalizationError, "number tokens"):
        validate_localized_text(source, translated)

 def test_preflight_rejects_an_all_english_display_section(self):
    source = product()
    with self.assertRaisesRegex(ResearchLocalizationError, "no Simplified Chinese"):
        validate_localized_text(source, {"sections": [{
            "index": 0, "title": "Demand",
            "body": "Revenue was USD 1,234.50, up 5.2%.",
            "gaps": ["Missing FY2027 margin"],
        }]})

 def test_internal_section_ids_and_thesis_labels_are_not_financial_numbers(self):
    source = product()
    source['sections'][0].update(title='causal_chain:0', body='T1 supports revenue of 123 USD.', gaps=[])
    translated={'sections':[{'index':0,'title':'收入增长的传导','body':'T1 支持 123 USD 收入的判断，需继续跟踪 T1。','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,translated)[0]['index'],0)
    translated['sections'][0]['body']='T1 支持 124 USD 收入的判断。'
    with self.assertRaises(ResearchLocalizationError):
        validate_localized_text(source,translated)
 def test_excel_titles_and_explanations_may_remain_english(self):
    source = product(); source["kind"] = "model_excel"
    checked = validate_localized_text(source, {"sections": [{
        "index": 0, "title": "Financial Model",
        "body": "Revenue was USD 1,234.50, up 5.2%.",
        "gaps": ["Missing FY2027 margin"],
    }]})
    self.assertEqual(checked[0]["title"], "Financial Model")

 def test_language_revision_can_use_digits_for_chinese_counts_and_months(self):
    source=product();source['sections'][0].update(title='订单',body='GIS订单出货比低于一；十二月继续跟踪。对T1与增长的影响尚待明确。',gaps=[])
    out={'sections':[{'index':0,'title':'订单','body':'GIS 订单出货比低于1；12月继续跟踪。对 T1 与增长的影响尚待明确。','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,out)[0]['index'],0)

 def test_internal_s_labels_and_chinese_adjacent_english_months_are_not_new_facts(self):
    source=product();source['sections'][0].update(
        title='S1 公司概览',body='公司于November 2021完成交易。',gaps=[])
    out={'sections':[{'index':0,'title':'公司概览',
        'body':'公司于November 2021（2021年11月）完成交易。','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,out)[0]['index'],0)

 def test_quarter_labels_allow_chinese_names_and_exact_period_consolidation(self):
    source=product();source['sections'][0].update(title='进展',body=(
        'IBM Q2 improved before early Q3. From 2026Q1 to 2026Q2, A improved; '
        'from 2026Q1 to 2026Q2, B improved.'),gaps=[])
    out={'sections':[{'index':0,'title':'进展','body':(
        'IBM第二季度改善，第三季度初继续。2026Q1至2026Q2期间，A和B均改善。'),
        'gaps':[]}]}
    self.assertEqual(validate_localized_text(source,out)[0]['index'],0)
    out['sections'][0]['body']='IBM第二季度改善，第四季度初继续。2026Q1至2026Q2期间，A和B均改善。'
    with self.assertRaisesRegex(ResearchLocalizationError,'number tokens'):
        validate_localized_text(source,out)

 def test_above_parity_book_to_bill_alone_may_be_shown_as_above_one(self):
    source=product();source['sections'][0].update(title='订单',
        body='The book-to-bill was above-parity; conversion remains uncertain.',gaps=[])
    out={'sections':[{'index':0,'title':'订单',
        'body':'订单收入比高于1，但转化仍不确定。','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,out)[0]['index'],0)
    source['sections'][0]['body']='Conversion was above-parity; book-to-bill was discussed elsewhere.'
    with self.assertRaisesRegex(ResearchLocalizationError,'number tokens'):
        validate_localized_text(source,out)

 def test_financial_period_shorthand_and_cents_have_bounded_equivalents(self):
    cases=(
      ('Since C4Q24, LTM bookings improved.','自2024年第四季度以来，过去12个月签约额改善。'),
      ('The average over 4 quarters was stable.','四个季度的平均值稳定。'),
      ('FY26 improves through FY29.','2026财年改善并延续至2029财年。'),
      ('Cost was $0.03 and the next cost was $0.02.','成本为3美分，下一项为2美分。'),
      ('whole-site 403 as of 2026-09; pre-2023 forms differ.',
       '截至2026年9月整站返回403；2023年以前的表格不同。'),
      ('C4Q24 remained labelled C4Q24.','2024年第四季度仍标记为C4Q24。'),
      ('In 2026, FY26 improved.','2026年，2026财年改善。'),
      ('The window is 2–4 quarters and covers 8 个未来季度.',
       '窗口为二至四个季度，覆盖未来八个季度。'),
    )
    for original,translated in cases:
      source=product();source['sections'][0].update(title='期间',body=original,gaps=[])
      out={'sections':[{'index':0,'title':'期间','body':translated,'gaps':[]}]}
      self.assertEqual(validate_localized_text(source,out)[0]['index'],0)

 def test_repeated_calendar_year_may_be_omitted_but_dates_may_not_change(self):
    source=product();source['sections'][0].update(title='期间',
      body='2025-07-01..2025-09-30 and 2025-10-01..2025-12-31',gaps=[])
    out={'sections':[{'index':0,'title':'期间',
      'body':'2025年7月1日至9月30日，以及10月1日至12月31日','gaps':[]}]}
    self.assertEqual(validate_localized_text(source,out)[0]['index'],0)
    out['sections'][0]['body']='2025年7月1日至9月30日，以及10月2日至12月31日'
    with self.assertRaisesRegex(ResearchLocalizationError,'number tokens'):
      validate_localized_text(source,out)
