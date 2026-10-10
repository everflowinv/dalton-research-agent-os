from __future__ import annotations
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from dalton_core.cockpit_plane import CockpitPlane, _job_recovery_fields
from tests.test_cockpit_plane import CockpitHarness
from tests.test_cockpit_v2_shell import node,V2
from tests.test_cockpit_analyst_ux_2026_10_09 import fixtures

ERROR='CockpitModelError: evidence and question exceed the model input bound'
RECOVERY=_job_recovery_fields('ask','failed',ERROR)['recovery']

class RecoveryProjectionTests(unittest.TestCase):
    def test_exact_source_code_only_and_raw_job_unchanged(self):
        job={'kind':'ask','status':'failed','error':ERROR,'result':None}
        before=copy.deepcopy(job);public=CockpitPlane._public_job(job)
        self.assertEqual(public['recovery'],RECOVERY);self.assertEqual(job,before)
        self.assertEqual(public['error'],ERROR)
        for error in ('timeout','资料太多，请重试','model input unavailable'):
            self.assertTrue(_job_recovery_fields('ask','failed',error)['recovery']['retryable'])
        self.assertEqual(_job_recovery_fields('goal','failed',ERROR),{})
        self.assertEqual(_job_recovery_fields('ask','done',None),{})
    def test_disk_job_and_history_project_same_recovery_without_journal_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            harness=CockpitHarness(Path(d))
            try:
                plane=harness.plane;job_id='cockpit-job:synthetic-bounded'
                plane.journal.write('INSERT INTO cockpit_jobs(job_id,kind,login,status,request_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                    (job_id,'ask','owner@example.com','failed','{"question":"范围过宽","request_id":"old-id"}',ERROR,'t1','t1'))
                before=plane.journal.rows('SELECT * FROM cockpit_jobs WHERE job_id=?',(job_id,))[0]
                self.assertEqual(plane.job('owner@example.com',job_id)['recovery'],RECOVERY)
                self.assertEqual(plane.history('owner@example.com','ask')[0]['recovery'],RECOVERY)
                after=plane.journal.rows('SELECT * FROM cockpit_jobs WHERE job_id=?',(job_id,))[0]
                self.assertEqual(dict(after),dict(before))
            finally:harness.close()
    def test_shell_job_error_retains_structured_recovery_and_raw_error(self):
        script="""import {createApi} from MODULE;
const body={kind:'ask',status:'failed',error:ERROR,display_message:'缩小范围后再问',recovery:RECOVERY};
const api=createApi({fetchImpl:async()=>({ok:true,status:200,json:async()=>body})});
try{await api.job('synthetic',null,{intervalMs:0});}catch(error){console.log(JSON.stringify(error.body));}
""".replace('MODULE',json.dumps((V2/'shell.js').as_uri())).replace('ERROR',json.dumps(ERROR)).replace('RECOVERY',json.dumps(RECOVERY))
        result=subprocess.run(['node','--input-type=module','-e',script],capture_output=True,text=True,check=True)
        value=json.loads(result.stdout);self.assertEqual(value['error'],ERROR);self.assertEqual(value['recovery'],RECOVERY)
    def test_failed_history_narrows_only_permanent_and_transient_keeps_retry(self):
        permanent={'job_id':'old1','status':'failed','request':{'question':'范围过宽','request_id':'id1'},'recovery':RECOVERY,'created_at':'t1'}
        transient={'job_id':'old2','status':'failed','request':{'question':'临时网络错误','request_id':'id2'},'created_at':'t2'}
        for row,narrow in ((permanent,True),(transient,False)):
            f=fixtures(**{'/v1/cockpit/history?kind=ask':{'items':[row]}})
            text=node('pages',{'fixtures':f,'render':{'a':'#/ask'}})['results']['a']['text']
            self.assertEqual('原问题直接重试' in text,not narrow)
            if narrow:self.assertIn('缩小范围后重新提问',text)
    def test_live_permanent_failure_clears_replay_and_focuses_question_without_post(self):
        f=fixtures(**{'/v1/cockpit/history?kind=ask':{'items':[]}})
        out=node('pages',{'fixtures':f,'render':{'a':'#/ask'},
            'drafts':{'ask:pending':{'question':'范围过宽','request_id':'old-id','job_id':'old-job'}},
            'jobError':'缩小范围后再问','jobErrorBody':{'error':ERROR,'recovery':RECOVERY},
            'steps':[{'page':'a','op':'wait'},{'page':'a','op':'click','text':'缩小范围后重新提问'}, {'page':'a','op':'submit','tag':'form'}]})
        after=out['results']['a:after'];self.assertNotIn('原问题直接重试',after['text'])
        self.assertNotIn('ask:pending',after['drafts']);self.assertEqual(after['focused'],'ask-question')
        self.assertEqual(out['posted'],[])

class DisplayCopyTests(unittest.TestCase):
    def test_prefix_only_not_financial_content_or_official_source_mutation(self):
        strings=['基准情景：基准：营业收入增长 3%','排序基准：按同一期间排序','基准假设仍待核对','风险：基准情景可能变化']
        out=node('call',{'module':'pages/company_compare.js','calls':[['baselineContent',x] for x in strings]})
        self.assertEqual([x['ok'] for x in out],['营业收入增长 3%','按同一期间排序',strings[2],strings[3]])
    def test_local_delivery_is_saved_and_keeps_actual_timestamp_remote_stays_delivered(self):
        view={'cycles':[{'issue_version_ref':'v1','destination_ref':None,'scheduled_for':'2026-10-10T00:00:00Z'},
                        {'issue_version_ref':'v2','destination_ref':'openclaw:slack:default:channel:remote','scheduled_for':'2026-10-10T00:00:00Z'}],
              'deliveries':[{'issue_version_ref':'v1','delivered_at':'2026-10-10T00:05:00Z'},
                            {'issue_version_ref':'v2','delivered_at':'2026-10-10T00:06:00Z'}]}
        rows=node('call',{'module':'pages/weekly_brief.js','calls':[['briefHistory',view]]})[0]['ok']
        self.assertEqual([r['state'] for r in rows],['saved','delivered'])
        self.assertEqual([r['delivered_at'] for r in rows],[d['delivered_at'] for d in view['deliveries']])
        view['deliveries']=[]
        rows=node('call',{'module':'pages/weekly_brief.js','calls':[['briefHistory',view,0]]})[0]['ok']
        self.assertTrue(all(r['delivered_at'] is None and r['state']=='pending' for r in rows))
    def test_local_weekly_body_h1_rendered_once_and_bullets_as_list_without_raw_change(self):
        body='# 研究进展周报\n\n本期进展。\n\n- 第一项资料缺口\n- 第二项资料缺口\n\n## 后续事项\n\n保留正文。'
        document={'issue_version_ref':'v1','title':'研究进展周报','period':{'start':'2026-10-03T00:00:00Z','end':'2026-10-10T00:00:00Z'},'body':body}
        view={'enabled':True,'cycles':[{'issue_version_ref':'v1','brief_kind':'mission_progress','destination_ref':None,'scheduled_for':'2026-10-10T00:00:00Z'}],
              'deliveries':[{'issue_version_ref':'v1','delivered_at':'2026-10-10T00:05:00Z'}]}
        out=node('pages',{'fixtures':fixtures(**{'/v1/cockpit/weekly-brief':view,'/v1/cockpit/weekly-brief-document':document}),
            'render':{'s':'#/settings?focus=brief'},'steps':[{'page':'s','op':'click','text':'查看周报'}]})
        result=out['results']['s'];self.assertEqual(result['documentLists'],[['第一项资料缺口','第二项资料缺口']])
        text=result['text'];self.assertIn('已保存',text);self.assertIn('保存时间',text);self.assertNotIn('已送达',text)
        self.assertNotIn('研究进展周报',result['h2']) # body H1 is not duplicated below the reader title
        self.assertEqual(document['body'],body)
