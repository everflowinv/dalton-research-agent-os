from __future__ import annotations
import json,threading,time,unittest,shutil,subprocess,tempfile
from pathlib import Path
from unittest.mock import patch
from tests import test_workspace_mission_setup as fixtures
from dalton_core.store import content_hash
from dalton_core.workspace_mission_setup import plan_first_mission_goal,goal_company_candidates,first_mission_goal_prompt,WorkspaceMissionSetupError

class GoalCompanyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WorkspaceFirstMissionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
    def draft(self, goal, rows, resolver, **scope):
        wire={'summary':'范围建议','title':'研究农业产业链','objective':goal,'industry':{'name':'农业产业链','reason':'用户研究目标'},'research_questions':['哪些环节受益？'],'subtasks':['核对申报'],'suggested_companies':rows,**scope}
        calls=[]
        class Model:
            def call_setup(_,**kw):calls.append(kw);return {'text':json.dumps(wire)}
        d=plan_first_mission_goal(Model(),self.fixture.workspace,goal=goal,method_foundation=self.fixture.foundation,request_id='unique-goal',created_at='2026-10-11T00:00:00Z',company_resolver=resolver)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0]['planning_context']['budget']['max_daily_paid_calls'],6)
        self.assertEqual(d['content_hash'],content_hash({k:v for k,v in d.items()if k!='content_hash'}));return d
    @staticmethod
    def row(ticker='AGR',mention=None):
        return {'ticker':ticker,'name':'Synthetic Farm Supplier','industry_role':'生产资料','reason':'比较农业设备需求与投入品成本的敏感度','owner_mention':mention}
    @staticmethod
    def issuer(ticker):return {'ticker':ticker,'cik':'12345','name':'Synthetic Registered Issuer'}
    def test_clear_industry_without_named_ticker_proposes_verified_reviewable_companies(self):
        d=self.draft('研究美国农业产业链股票', [self.row()],self.issuer,company_selection='industry_recommendation',owner_company_mentions=[])
        self.assertEqual(d['setup_state'],'ready_for_confirmation');self.assertEqual(d['mission_body']['universe'][0]['ticker'],'AGR');r=d['company_recommendations'][0];self.assertEqual(r['name'],'Synthetic Registered Issuer');self.assertTrue(r['included']);self.assertEqual(r['identity_hash'],content_hash({'ticker':'AGR','cik':'0000012345','name':'Synthetic Registered Issuer'}));self.assertEqual(r['reason'],self.row()['reason']);self.assertIsNone(d['mission_body']['bindings'])
    def test_one_failed_automatic_candidate_does_not_block_verified_candidates(self):
        def resolve(t):
            if t=='MISS':raise TimeoutError('private detail')
            return self.issuer(t)
        d=self.draft('研究农业产业链股票',[self.row(),self.row('MISS')],resolve,company_selection='industry_recommendation',owner_company_mentions=[])
        self.assertEqual(d['setup_state'],'ready_for_confirmation');self.assertEqual([r['ticker']for r in d['mission_body']['universe']],['AGR']);self.assertFalse(d['company_recommendations'][1]['included']);self.assertNotIn('private detail',str(d));self.assertNotIn('TimeoutError',str(d))
    def test_plain_ticker_named_scope_failure_retains_requested_company(self):
        def resolve(t):
            if t=='GOOGL':raise ValueError('missing')
            return self.issuer(t)
        d=self.draft('只研究 MSFT、GOOGL',[self.row('MSFT','MSFT'),self.row('GOOGL','GOOGL'),self.row('AGR')],resolve,company_selection='named_only',owner_company_mentions=['MSFT','GOOGL'])
        self.assertEqual(d['setup_state'],'review_required');self.assertEqual([r['ticker']for r in d['company_recommendations']],['MSFT','GOOGL']);self.assertTrue(d['company_recommendations'][1]['requested']);self.assertFalse(d['company_recommendations'][1]['included'])
    def test_chinese_named_scope_partial_failure_never_silently_drops_name(self):
        def resolve(t):
            if t=='GOOGL':raise ValueError('missing')
            return self.issuer(t)
        d=self.draft('只研究微软和谷歌',[self.row('MSFT','微软'),self.row('GOOGL','谷歌')],resolve,company_selection='named_only',owner_company_mentions=['微软','谷歌'])
        self.assertEqual(d['setup_state'],'review_required');self.assertEqual([r['owner_mention']for r in d['company_recommendations']],['微软','谷歌'])
    def test_model_omitted_named_company_is_retained_as_pending_without_lookup_or_authority(self):
        d=self.draft('只研究微软和谷歌',[self.row('MSFT','微软')],self.issuer,company_selection='named_only',owner_company_mentions=['微软','谷歌'])
        self.assertEqual(d['setup_state'],'review_required');self.assertEqual(d['company_recommendations'][1]['owner_mention'],'谷歌');self.assertEqual(d['company_recommendations'][1]['ticker'],'');self.assertFalse(d['company_recommendations'][1]['included'])
    def test_mixed_symbol_and_chinese_name_are_both_preserved(self):
        def resolve(t):
            if t=='GOOGL':raise ValueError('missing')
            return self.issuer(t)
        d=self.draft('只研究 $MSFT 和谷歌',[self.row('MSFT','MSFT'),self.row('GOOGL','谷歌')],resolve,company_selection='named_only',owner_company_mentions=['MSFT','谷歌'])
        self.assertEqual([r['ticker']for r in d['company_recommendations']],['MSFT','GOOGL']);self.assertEqual(d['setup_state'],'review_required');self.assertTrue(d['company_recommendations'][1]['requested'])
    def test_acronyms_in_industry_objective_do_not_become_named_stock_symbols(self):
        d=self.draft('研究农业产业链，关注 ENSO、FCF、RPO 和 GAAP 口径',[self.row()],self.issuer,company_selection='industry_recommendation',owner_company_mentions=[])
        self.assertEqual(d['setup_state'],'ready_for_confirmation');self.assertEqual([r['ticker']for r in d['mission_body']['universe']],['AGR'])
    def test_missing_explicit_ticker_resolves_to_valid_ref_without_new_model_call(self):
        d=self.draft('只研究 $MSFT $GOOGL',[self.row('MSFT')],self.issuer,company_selection='named_only',owner_company_mentions=['MSFT','GOOGL'])
        self.assertEqual(d['setup_state'],'ready_for_confirmation');self.assertEqual({r['company_ref']for r in d['mission_body']['universe']},{'company:ticker:msft','company:ticker:googl'})
        self.assertTrue(d['company_recommendations'][1]['requested']);self.assertTrue(d['company_recommendations'][1]['included']);self.assertIsNone(d['company_recommendations'][1]['identity_error'])
    def test_forged_owner_mention_is_rejected(self):
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'exact words'):
            self.draft('研究农业行业',[self.row()],self.issuer,company_selection='named_only',owner_company_mentions=['not in objective'])
    def test_identity_mismatch_cannot_enter_universe(self):
        d=self.draft('研究农业行业',[self.row()],lambda t:{**self.issuer(t),'ticker':'OTHER'},company_selection='industry_recommendation',owner_company_mentions=[])
        self.assertEqual(d['mission_body']['universe'],[]);self.assertEqual(d['setup_state'],'review_required')
    def test_candidate_checks_are_capped_and_only_three_concurrent(self):
        lock=threading.Lock();active=0;maximum=0;seen=[]
        def resolve(t):
            nonlocal active,maximum
            with lock:active+=1;maximum=max(maximum,active);seen.append(t)
            time.sleep(.01)
            with lock:active-=1
            return self.issuer(t)
        rows=goal_company_candidates([self.row('AG'+chr(65+i))for i in range(12)],goal='研究农业产业链',workspace=self.fixture.workspace,company_resolver=resolve)
        self.assertEqual(len(rows),8);self.assertEqual(len(seen),8);self.assertLessEqual(maximum,3)
    def test_identity_deadline_does_not_start_unbounded_lookup(self):
        with patch('time.monotonic',side_effect=[0,36,36]),patch('dalton_core.workspace_mission_setup.resolve_sec_ticker')as resolver:
            rows=goal_company_candidates([self.row(),self.row('AGRZ')],goal='研究农业产业链',workspace=self.fixture.workspace,sec_resolver_identity='Synthetic operator')
        resolver.assert_not_called();self.assertTrue(all(not r['included']for r in rows))
    def test_prompt_requires_independent_checks_and_exact_scope_without_additional_setup_call(self):
        p=first_mission_goal_prompt('研究农业产业链');self.assertIn('missing owner-supplied tickers alone',p);self.assertIn('owner_company_mentions',p);self.assertIn('preliminary business judgments',p);self.assertIn('do not add other companies',p)

    def test_actual_draft_view_keeps_named_pending_rows_and_publish_only_after_review(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copytree(Path(__file__).resolve().parents[1]/'src/dalton_core/cockpit_v2',root/'ui');(root/'package.json').write_text('{"type":"module"}')
            script="""
import assert from 'node:assert/strict';
globalThis.window={DaltonV2:{registerPage(){}}};
const {draftView}=await import('./ui/pages/settings.js');
const h=(tag,attrs,...children)=>({tag,attrs,children:children.flat(Infinity).filter(x=>x!==null&&x!==undefined),append(...x){this.children.push(...x)}});
const buttons=[];const ctx={h,say:x=>x,fmt:{usd:x=>String(x)},ui:{kv:rows=>h('dl',null,rows.flat()),note:x=>h('p',null,x),button:label=>{buttons.push(label);return h('button',null,label)}}};
const draft={schema_version:'workspace-first-mission-draft-0.1',setup_state:'review_required',mission_body:{universe:[],research_questions:[]},company_recommendations:[{name:'Synthetic Issuer',ticker:'SYN',included:true,cik:'0000012345',reason:'Equipment demand'},{suggested_name:'Named company',ticker:'MISS',requested:true,included:false,identity_error:'公开股票资料暂未核对成功'}]};
const tree=draftView(ctx,{draft},null);const text=JSON.stringify(tree);
assert.ok(text.includes('研究目标与覆盖范围草案'));assert.ok(text.includes('指定公司待核对'));assert.ok(text.includes('建议覆盖理由'));assert.ok(text.includes('公司资料'));assert.ok(text.includes('CIK=0000012345'));assert.equal(buttons.length,0);
draft.setup_state='ready_for_confirmation';draftView(ctx,{draft},null);assert.ok(buttons.includes('确认覆盖范围并启动研究'));
"""
            (root/'check.mjs').write_text(script);result=subprocess.run([node,str(root/'check.mjs')],capture_output=True,text=True,timeout=15);self.assertEqual(result.returncode,0,result.stderr)
