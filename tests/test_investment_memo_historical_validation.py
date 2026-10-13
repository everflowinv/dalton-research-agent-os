"""Paid history remains authoritative when current number validation improves."""
import copy
import json
import re
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core import investment_memo_group_maintenance as gm
from dalton_core import mission_deliverable as md
from dalton_core.investment_memo_draft import build_group_prompt,GROUPS
from dalton_core.scheduler import Scheduler
from tests import test_investment_memo_group_maintenance as fixtures


# Exact pre-0.9 token behavior, frozen to create actual historical refused
# answers even when this test runs under an improved current validator.
_LEGACY_PERIOD_TOKEN_RE = re.compile(
    # ISO dates and the ".." ranges the Ledger prints for a reporting period,
    # which live were being read apart into "01" and "31".
    r"(?:19|20)\d{2}-\d{2}-\d{2}(?:\s*\.\.\s*(?:19|20)?\d{2}-\d{2}-\d{2})?"
    r"|(?:FY\s?)?(?:19|20)\d{2}(?:\s?[-–/]\s?(?:19|20)?\d{2})?(?:\s?(?:年|财年))?"
    r"|FY\s?\d{2}(?![0-9])"
    r"|Q[1-4]\s?(?:FY\s?)?(?:19|20)?\d{0,4}"
    r"|(?:19|20)\d{2}\s?Q[1-4]"
    r"|[1-4]Q(?:19|20)?\d{2}"
    r"|H[12]\s?(?:19|20)?\d{0,4}"
    r"|第?[一二三四1-4]季度"
    # Batch D: a rolling-window label is a period, not a figure -- 「30 日 ADV」,
    # 「52 周区间」, "30-day ADV". Narrow on purpose: only when a window noun
    # follows, so 「30 日内上涨 5%」's 30 is still a figure to source.
    r"|\d{1,3}\s?(?:日|天|周|个交易日)(?=\s?(?:ADV|均量|成交|区间|高点|低点|滚动|移动|平均|波动))"
    r"|\d{1,3}[-\s]?(?:day|week)(?=\s?(?:ADV|average|moving|volume|range|high|low))",
    re.IGNORECASE,
)

def _legacy_value_tokens(text: str, *, include_small: bool = False) -> list[str]:
    """Every figure the text asserts, with period labels and citation tags removed."""

    without_tags = md._CITATION_TAG_RE.sub(" ", text or "")
    without_periods = _LEGACY_PERIOD_TOKEN_RE.sub(" ", without_tags)
    tokens = []
    for match in md._VALUE_TOKEN_RE.finditer(without_periods):
        token = match.group(0).strip()
        plain = token.rstrip(".")
        if not include_small and plain.isdigit() and int(plain) <= md._BARE_SMALL_INTEGER:
            continue
        tokens.append(token)
    return tokens


class MemoHistoricalValidationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MemoGroupMaintenanceTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def test_current_validator_pass_does_not_revoke_paid_history_or_authorize_second_grant(self):
        f=self.f;wrapper,outcome=f.seeded();body,value=f.purchase(wrapper,outcome)
        old=f.fees();calls=len(f.adapter.served)
        with patch.object(gm,'validate_answer',return_value={'status':'drafted'}):
            with Scheduler(f.model.scheduler_db) as s:
                entries=gm.read_entries(wrapper.macro,s.connection)
            self.assertEqual(entries,[body['entry']])
            fresh=f.wrapper()
            self.assertIsNone(fresh.macro_error)
            self.assertEqual(f.outcome(fresh,1)['status'],'refused') # returned parent still needs actual full acceptance
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),calls)
        with Scheduler(f.model.scheduler_db) as s:self.assertEqual(s.connection.execute(f'SELECT count(*) FROM {gm.TABLE}').fetchone()[0],1)

    def test_source_deadline_is_distinct_from_parent_now_passing_and_cannot_buy(self):
        f=self.f;wrapper,outcome=f.seeded();old=f.fees();calls=len(f.adapter.served)
        for kind in ('source_unavailable','authority_unavailable'):
            with patch.object(gm,'validate_answer',return_value={'status':'refused','failure_class':kind,'reason':'original_verification_timeout'}):
                with self.assertRaisesRegex(ValueError,kind+': original_verification_timeout'):
                    gm.derive(wrapper.macro,f.contexts[1],outcome['model']['work_order_ref'])
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),calls)

    def test_legacy_record_reconstructs_exact_prompt_and_hash_without_current_diagnostic(self):
        f=self.f;wrapper,outcome=f.seeded()
        with patch.object(gm,'CONTRACT_REF',gm.LEGACY_CONTRACT_REF),patch.object(gm,'_validation_contracts',return_value=['product-validation:exact-rendered-passages-bound-arithmetic:1','section-numbers-capacity:128:v1']):
            body,value=f.purchase(wrapper,outcome)
        before=copy.deepcopy(body)
        with patch.object(gm,'validate_answer',side_effect=AssertionError('current diagnosis is not historical authority')):
            with Scheduler(f.model.scheduler_db) as s:entries=gm.read_entries(wrapper.macro,s.connection)
        self.assertEqual(entries,[body['entry']]);self.assertEqual(body,before)
        self.assertNotIn('number-source-contract',json.dumps(body['validation_contracts']))

    def test_legacy_paid_maintenance_reentry_does_not_reissue_using_current_contract(self):
        f=self.f;wrapper,outcome=f.seeded()
        with patch.object(gm,'CONTRACT_REF',gm.LEGACY_CONTRACT_REF),patch.object(gm,'_validation_contracts',return_value=['product-validation:exact-rendered-passages-bound-arithmetic:1','section-numbers-capacity:128:v1']):
            body,value=f.purchase(wrapper,outcome)
        old=f.fees();paid=len(f.adapter.served)
        fresh=f.wrapper();again=f.outcome(fresh,1)
        self.assertNotIn('memo_macro_fee_audit_required',again.get('reason',''))
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),paid)
        kwargs={'purpose':'investment_memo','request_id':body['entry']['request_id'],
                'prompt':body['prompt'],'mission':f.mission}
        with patch.object(gm,'validate_answer',side_effect=AssertionError('no new permission')):
            fresh.macro.register_maintenance(body,kwargs)
        forged=copy.deepcopy(body);forged['diagnostics']['reason']='caller replacement'
        with self.assertRaisesRegex(ValueError,'stored diagnostics/source'):
            fresh.macro.register_maintenance(forged,kwargs)
        self.assertEqual(f.fees(),old)

    def test_unknown_historical_contract_and_foreign_diagnostics_fail_closed(self):
        f=self.f;wrapper,outcome=f.seeded();body,value=f.purchase(wrapper,outcome)
        for mutation in ('contract','numeric','source','section','raw'):
            bad=copy.deepcopy(body)
            if mutation=='contract':bad['contract_ref']='unknown:1'
            elif mutation=='numeric':bad['validation_contracts'][-1]='number-source-contract:future'
            elif mutation=='source':bad['diagnostics']['refs']=['source:foreign']
            elif mutation=='section':bad['diagnostics']['findings'][0]['section']='unrelated group'
            else:bad['raw_response_hash']='0'*64
            try:
                rebuilt=gm._replay_issued(wrapper.macro,bad)
            except ValueError:continue
            self.assertNotEqual(rebuilt,bad)

    def test_actual_long_market_scalar_old_parent_can_resume_free_after_numeric_fix(self):
        f=self.f;f.bad_groups=set()
        for context in f.contexts:context['material'].append({'ref':'market:synthetic','kind':'market_block','text':'市值 131916360437 USD'})
        f.prompts=[build_group_prompt(**context)for context in f.contexts]
        execute=f.adapter.execute
        def answer(work,route,profile):
            invocation,envelope=execute(work,route,profile)
            value=json.loads(envelope.outputs['text'])
            if '\nGroup: view\n' in work.question:
                if '\nMaintenance contract:' in work.question:
                    value['sections'][0]['body']='无来源利润 888.1。'
                else:
                    value['sections'][0]['body']='市值换算已核对。'
                    value['sections'][0]['numbers']=[{'text':'市值 131916360437 USD','number_ref':'market:synthetic','period':'2026-10-12'},
                        {'text':'1319.16360437 亿美元','period':'2026-10-12','derivation':{'expression':'131916360437/100000000','result':'1319.16360437'}}]
            return invocation,replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
        f.adapter.execute=answer
        current_tokens=md.value_tokens
        with patch.object(md,'value_tokens',_legacy_value_tokens),patch.object(md,'NUMBER_SOURCE_CONTRACT_VERSION','number-source-contract:0.8'):
            self.assertEqual(md.value_tokens('市值 131916360437 USD'),['13','360437'])
            wrapper,outcome=f.seeded()
            self.assertIn('131916360437',outcome['reason'])
            with patch.object(gm,'CONTRACT_REF',gm.LEGACY_CONTRACT_REF),patch.object(gm,'_validation_contracts',return_value=['product-validation:exact-rendered-passages-bound-arithmetic:1','section-numbers-capacity:128:v1']):
                body,bad=f.purchase(wrapper,outcome)
        self.assertEqual(bad['status'],'refused');old=f.fees();calls=len(f.adapter.served)
        # The narrow fixed token behavior: years/dates stay periods; a year
        # substring inside a longer scalar must not split the sourced value.
        def corrected(text,*,include_small=False):
            if text=='市值 131916360437 USD':return ['131916360437']
            return current_tokens(text,include_small=include_small)
        with patch.object(md,'value_tokens',corrected),patch.object(md,'NUMBER_SOURCE_CONTRACT_VERSION','number-source-contract:0.9'):
            fresh=f.wrapper();value=f.outcome(fresh,1)
            self.assertEqual(value['status'],'drafted')
            self.assertEqual(value['model']['work_order_ref'],outcome['model']['work_order_ref'])
            self.assertEqual(fresh.macro.last['historical_work_count'],4)
            with self.assertRaisesRegex(md.MissionDeliverableConflict,'131916360438'):
                md.validate_derived_number({'text':'1319.16360438','derivation':{'expression':'131916360438/100000000','result':'1319.16360438'}},[{'text':'市值 131916360437 USD'}])
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),calls)
        with Scheduler(f.model.scheduler_db)as s:
            self.assertEqual(gm.record_for(wrapper.macro,s.connection,1),body)

    def test_numeric_contract_changes_lane_identity_without_changing_paid_prompt(self):
        from dalton_core.mission_investment_memo_lane import company_signature
        f=self.f;frozen={'mission':f.mission,'playbook':{'id':'p:1','content_hash':'a'*64},'company':{'company_ref':'company:synthetic-a'},'input_bindings':[]}
        config_path=f.root/'memo-config.json'
        config_path.write_text(json.dumps(f.model.config))
        launcher=SimpleNamespace(model_config_path=config_path,verifier_model_config_path=config_path)
        with patch.object(md,'NUMBER_SOURCE_CONTRACT_VERSION','number-source-contract:0.8'):
            a=company_signature(frozen,launcher)
        with patch.object(md,'NUMBER_SOURCE_CONTRACT_VERSION','number-source-contract:0.9'):
            b=company_signature(frozen,launcher)
        self.assertNotEqual(a,b)
        self.assertEqual(f.prompts,[build_group_prompt(**context)for context in f.contexts])
