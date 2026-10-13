"""Held numeric annotation repair through the real finite owner/admission path."""
import copy
import unittest
from unittest import mock
from tests import test_initial_screen_content_maintenance as fixture_module
from tests.synthetic_company import material_pack, strl_document
from dalton_core import initial_screen_estimate_citation_repair as repair
from dalton_core import initial_screen_content_maintenance as maintenance
from dalton_core import initial_screen_validator_recovery as recovery
from dalton_core.store import content_hash
from dalton_core.product_contract import normalise_pack


def pack(company='company:ticker:other'):
    value=material_pack(company=company)
    value.json['company_ref']=company
    value.json['our_model'].update(status='available',years=[{'label':'FY2027','revenue':{
        'value':5700000000,'unit':'usd','status':'available','method':'annual_projection'}}])
    value.tags['N99']='pack-json:/our_model/years/0/revenue/value'
    value.json['number_tags']['N99']={'pointer':'/our_model/years/0/revenue/value',
        'value':5700000000,'display':'5.70bn','transform':'÷1e9'}
    value.text+='\n[N99] 我们的模型 FY2027 收入 5.70bn（5700000000）\n'
    value.content_hash=content_hash({'text':value.text,'tags':value.tags})
    return value


def missing():
    return {'estimate':'assumption:N99','text':'我们的估计FY2027收入高于一致预期',
        'metric':'营业收入','period':'FY2027','unit':'十亿美元'}


class EstimateCitationRepairTests(unittest.TestCase):
    def fixture(self,company='synw'):
        fcase=fixture_module.ContentMaintenanceTests('runTest')
        self.addCleanup(fcase.doCleanups)
        def document():
            value=strl_document();value['sections'][5]['numbers'].append(missing());return value
        with mock.patch.object(fixture_module,'material_pack',side_effect=lambda **kw:pack(**kw)), \
                mock.patch.object(fixture_module,'strl_document',side_effect=document):
            f=fcase.fixture(company)
        return f

    def corrected(self,result):
        raw=result['slots']['document:1'][1]
        allowed=maintenance.plan(raw,result['record']['findings'],result['context']['pack'])
        value=copy.deepcopy(raw);value['sections'][0]['claims']=['C1']
        for i,rule in allowed['edits'].items():
            for j,proof in rule.get('estimate_citations',{}).items():
                value['sections'][int(i)]['numbers'][int(j)].update(
                    estimate=proof['replacement_estimate'],text=proof['replacement_text'])
        return value,allowed

    def test_two_companies_paid_parent_preserved_real_owner_repair_verifier_restart(self):
        for company in ('synw','synz'):
            with self.subTest(company=company):
                f=self.fixture(company);result=f.qualify();before=len(f.f.adapter.served)
                raw=result['slots']['document:1'][1];fixed,allowed=self.corrected(result)
                self.assertTrue(any(x.get('detail')==repair.ERROR for x in result['record']['findings']))
                maintenance.assert_changes(raw,fixed,allowed,f.f.pack)
                f.f.adapter.value=fixed
                actual=maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
                self.assertEqual(actual['record']['findings'],[])
                self.assertEqual(actual['record']['paid_prefix'],result['record']['paid_prefix'])
                self.assertEqual(len(f.f.adapter.served),before+1)
                f.f.adapter.value={'verdict':'pass','findings':[]}
                verdict,call=recovery.verifier(actual,model=f.model,state=f.state,mission=f.f.mission)
                self.assertEqual(verdict['verdict'],'pass')
                restarted=maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
                _,replayed=recovery.verifier(restarted,model=f.model,state=f.state,mission=f.f.mission)
                self.assertEqual(call['work_order_ref'],replayed['work_order_ref'])
                self.assertEqual(len(f.f.adapter.served),before+2)
                self.assertEqual(raw['sections'][5]['numbers'][-1],missing())

    def test_exact_tag_missing_figure_source_alias_and_other_currency(self):
        p=normalise_pack(pack());n=missing();proof=repair._source(n,p)
        self.assertEqual(proof['source_value'],'5700000000');self.assertEqual(proof['replacement_estimate'],'N99')
        n['estimate']='N99';self.assertEqual(repair._source(n,p)['source_ref'],proof['source_ref'])
        p['json']['our_model']['years'][0]['revenue']['unit']='eur';n['unit']='十亿欧元'
        self.assertEqual(repair._source(n,p)['source_unit'],'eur')

    def test_wrong_tag_non_ours_missing_source_ambiguous_ref_refuse(self):
        base=normalise_pack(pack())
        for mutate in (lambda p,n:n.update(estimate='assumption:N999'),
                lambda p,n:n.update(estimate='assumption:ours:N99'),
                lambda p,n:p['tags'].update(N99='pack-json:/numbers/consensus/periods/0/revenue'),
                lambda p,n:p['json']['our_model'].update(years=[]),
                lambda p,n:p['json']['our_model'].update(status='unavailable'),
                lambda p,n:p['json']['our_model']['model'].update(version_ref='external:model'),
                lambda p,n:p['json']['our_model']['years'][0]['revenue'].update(value=1),
                lambda p,n:p['json'].update(company_ref='company:ticker:foreign'),
                lambda p,n:n.update(period='FY2028'),lambda p,n:n.update(metric='资产'),
                lambda p,n:n.update(unit='百万美元'),lambda p,n:n.update(text='收入 9.99bn')):
            p=copy.deepcopy(base);n=missing();mutate(p,n)
            with self.subTest(n=n),self.assertRaises(ValueError):repair._source(n,p)
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            repair.plan({'numbers':[missing(),missing()]},base)

    def test_edit_only_proved_pair_rejects_value_formula_period_source_body_and_forged_proof(self):
        f=self.fixture();result=f.qualify();raw=result['slots']['document:1'][1];fixed,allowed=self.corrected(result)
        for mutate in (lambda v:v['sections'][5]['numbers'][-1].update(text='我们的估计收入 9.99bn'),
                lambda v:v['sections'][5]['numbers'][-1].update(estimate='N19'),
                lambda v:v['sections'][5]['numbers'][-1].update(period='FY2028'),
                lambda v:v['sections'][5]['numbers'][-1].update(derivation={'expression':'1+1','result':'2'}),
                lambda v:v['sections'][5].update(body='我们的估计收入 5.70bn'),
                lambda v:v['sections'][0]['numbers'][0].update(unit='EUR')):
            value=copy.deepcopy(fixed);mutate(value)
            with self.assertRaises(ValueError):maintenance.assert_changes(raw,value,allowed,f.f.pack)
        forged=copy.deepcopy(allowed)
        next(iter(forged['edits']['5']['estimate_citations'].values()))['source_value']='1'
        with self.assertRaisesRegex(ValueError,'proof changed'):maintenance.assert_changes(raw,fixed,forged,f.f.pack)

    def test_used_fixed_slot_failed_or_unknown_cannot_buy_again_under_new_validator_budget(self):
        f=self.fixture();result=f.qualify();before=len(f.f.adapter.served)
        from dalton_core.model_router import ModelRouter
        import json
        with ModelRouter(f.f.router_db,read_only=True) as router:
            profiles=router.connection.execute('SELECT profile_json FROM model_endpoint_profile_versions').fetchall()
        f.f.adapter.script={json.loads(row[0])['id']:{'code':'TIMEOUT','message':'paid unknown'} for row in profiles}
        from dalton_core.cockpit_model import CockpitModelError
        with self.assertRaises(CockpitModelError):maintenance.run(result,model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        f.model.config['purpose_run_budgets']={'initial_screen':{'max_cost_usd':224}}
        with mock.patch.object(recovery,'validator_fingerprint',return_value='future'):
            with self.assertRaises(ValueError):maintenance.run(f.qualify(),model=f.model,state=f.state,mission=f.f.mission,authority=f.f.authority)
        self.assertEqual(len(f.f.adapter.served),before+1)

    def test_source_proof_uses_no_year_or_ref_digits_as_amount(self):
        p=normalise_pack(pack())
        for text in ('FY2027收入高于一致预期','我们的估计 FY2027 收入'):
            self.assertEqual(repair._source(dict(missing(),text=text),p)['replacement_estimate'],'N99')
        with self.assertRaises(ValueError):repair._source(dict(missing(),text='我们的估计收入为 2027 美元'),p)

    def test_legacy_issued_prompt_bytes_and_fixed_permission_are_unchanged(self):
        import hashlib
        from dalton_core.product_contract import normalise_pack
        p=normalise_pack(material_pack());raw=strl_document();raw['sections'][0]['claims']=['C999']
        findings=[{'code':'unknown_tag','section':'结论与观点','detail':'C999'}]
        allowed=maintenance.plan(raw,findings,p)
        result={'slots':{'document:1':({},raw)},'context':{'pack':p}}
        # Generated once from the frozen base665 _prompt, then confirmed
        # byte-equal to this implementation; no trusted hash was updated.
        self.assertEqual(hashlib.sha256(maintenance._prompt(result,allowed).encode()).hexdigest(),
            '5eec474a0eb49ff6afefd67829ca29d25f3cbbe6d8d88562d96474d8a985c718')
        f=self.fixture();r=f.qualify();permission=maintenance.permission(r)
        changed=copy.deepcopy(r);changed['record']['validator_fingerprint']='future-validator'
        self.assertEqual(maintenance.permission(changed),permission)

    def test_per_share_and_nonfinite_or_unavailable_source_remain_bounded(self):
        p=normalise_pack(pack());n=missing()
        p['json']['our_model']['years'][0]['eps']={'value':'5.00','unit':'usd_per_share',
            'status':'available','method':'annual_projection'}
        p['tags']['N99']='pack-json:/our_model/years/0/eps/value'
        p['json']['number_tags']['N99']={'pointer':'/our_model/years/0/eps/value','value':'5.00','display':'5.00'}
        n.update(metric='每股收益',unit='美元/股')
        self.assertEqual(repair._source(n,p)['source_unit'],'usd_per_share')
        for mutate in (lambda v:v['json']['our_model']['years'][0]['eps'].update(status='unavailable'),
                lambda v:v['json']['our_model']['years'][0]['eps'].update(value='NaN'),
                lambda v:v['json']['our_model']['years'][0]['eps'].update(unit='usd'),
                lambda v:v['json']['our_model']['years'][0]['eps'].update(method='invented')):
            bad=copy.deepcopy(p);mutate(bad)
            with self.assertRaises(ValueError):repair._source(n,bad)
