import copy
import shutil
import unittest
from tests.test_cockpit_v2_migration import fixtures, run, posts, H1

def scope_fixtures():
    f=fixtures();f['/v1/cockpit/levers']={'enabled':True,'mission_ref':'coverage-mission:synthetic','companies':[
        {'company_ref':'company:ticker:synw','ticker':'SYNW','label':'Synthetic Widgets','coverage_tier':'B'}]}
    f['/v1/cockpit/history?kind=coverage']={'drafts':[]}
    return f

DRAFT={'draft_id':'cockpit-draft:coverage:synthetic','draft_hash':H1,'draft':{
    'summary':'新增 OTHR；移除 SYNW','reason':'转向新产品领域','additions':[{'ticker':'OTHR','name':'Other Gadgets','cik':'99999','resolution':'resolved','coverage_tier':'B'}],
    'removals':[{'ticker':'SYNW','company_ref':'company:ticker:synw'}],'universe_after':[{'ticker':'OTHR'}],
    'plans':[{'source_ref':'source:sec-edgar','added':['OTHR'],'removed':['SYNW']}],
    'budget':{'max_daily_cost_usd':10,'coverage_pool_usd_per_company':{'before':2,'after':2},'note':'每日上限保持当前值。'}}}

@unittest.skipIf(shutil.which('node') is None,'node unavailable')
class ScopeTests(unittest.TestCase):
    def steps(self):return [{'page':'s','op':'set','id':'coverage-add','value':'OTHR'},
        {'page':'s','op':'set','id':'coverage-reason','value':'转向新产品领域'},
        {'page':'s','op':'check','id':'coverage-remove-SYNW'},
        {'page':'s','op':'submit','id':'coverage-form'}]

    def test_existing_writer_gets_explicit_changes_and_only_confirmation_publishes(self):
        out=run({'s':'#/settings?focus=scope'},self.steps(),fixtures=scope_fixtures(),postReplies={'/v1/cockpit/coverage_draft':DRAFT})
        p=posts(out,'/v1/cockpit/coverage_draft')[0]
        self.assertEqual((p['add'],p['remove'],p['reason'],p['coverage_tier']),('OTHR',['company:ticker:synw'],'转向新产品领域','B'))
        self.assertEqual(posts(out,'/v1/cockpit/publish'),[])
        self.assertEqual(out['jobs'],[])
        self.assertIn('新增 OTHR；移除 SYNW',out['results']['s:after']['text'])
        self.assertIn('coverage:draft',out['results']['s:after']['drafts'])
        out=run({'s':'#/settings?focus=scope'},[*self.steps(),{'page':'s','op':'click','text':'确认调整覆盖名单'}],fixtures=scope_fixtures(),postReplies={'/v1/cockpit/coverage_draft':DRAFT,'/v1/cockpit/publish':{'version':3}})
        self.assertEqual([{k:v for k,v in row.items() if k!='request_id'} for row in posts(out,'/v1/cockpit/publish')],[{'draft_id':DRAFT['draft_id'],'draft_hash':H1}])
        self.assertIn('第 3 版',out['results']['s:after']['text'])

    def test_reason_and_changes_are_required_and_form_survives_error(self):
        for steps in ([{'page':'s','op':'submit','id':'coverage-form'}],
                      [{'page':'s','op':'set','id':'coverage-reason','value':'理由'}, {'page':'s','op':'submit','id':'coverage-form'}]):
            out=run({'s':'#/settings'},steps,fixtures=scope_fixtures())
            self.assertEqual(out['posted'],[])
        out=run({'s':'#/settings'},self.steps(),fixtures=scope_fixtures(),postReplies={'/v1/cockpit/coverage_draft':{'error':'发行人暂时无法核对'}})
        self.assertIn('发行人暂时无法核对',out['results']['s:after']['text'])
        self.assertEqual(out['results']['s:after']['drafts']['coverage:add'],'OTHR')

    def test_open_server_draft_restores_without_recreation_and_discard_uses_coverage_kind(self):
        f=scope_fixtures();f['/v1/cockpit/history?kind=coverage']={'drafts':[{**DRAFT,'status':'open'}]}
        out=run({'s':'#/settings'},[{'page':'s','op':'click','text':'放弃名单变更'}],fixtures=f)
        self.assertEqual(posts(out,'/v1/cockpit/coverage_draft'),[])
        self.assertEqual([{k:v for k,v in row.items() if k!='request_id'} for row in posts(out,'/v1/cockpit/decide')],[{'kind':'draft:coverage','ref':DRAFT['draft_id'],'hash':H1,'decision':'discard'}])
        self.assertIn('已放弃',out['results']['s:after']['text'])

    def test_partial_plan_failure_keeps_the_published_version_and_names_the_gap(self):
        out=run({'s':'#/settings'},[*self.steps(),{'page':'s','op':'click','text':'确认调整覆盖名单'}],fixtures=scope_fixtures(),
                postReplies={'/v1/cockpit/coverage_draft':DRAFT,'/v1/cockpit/publish':{'version':3,'plan_errors':['SEC检索计划更新失败']}})
        text=out['results']['s:after']['text']
        self.assertIn('已发布为第 3 版',text)
        self.assertIn('1 项资料检索计划更新失败',text)
        self.assertIn('SEC检索计划更新失败',text)
        self.assertIn('名单变更已生效',text)

    def test_uncertain_network_result_keeps_the_draft_and_blocks_blind_retry(self):
        out=run({'s':'#/settings'},[*self.steps(),{'page':'s','op':'click','text':'确认调整覆盖名单'},
                {'page':'s','op':'click','text':'确认调整覆盖名单'}],fixtures=scope_fixtures(),
                postReplies={'/v1/cockpit/coverage_draft':DRAFT,'/v1/cockpit/publish':{'error':'网络连接中断','status':0}})
        self.assertEqual(len(posts(out,'/v1/cockpit/publish')),1)
        self.assertIn('尚未确认名单变更结果',out['results']['s:after']['text'])
        self.assertIn('coverage:draft',out['results']['s:after']['drafts'])
