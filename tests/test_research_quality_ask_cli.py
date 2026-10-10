import contextlib
import io
import json
import sqlite3
from unittest.mock import patch
from dalton_core.research_quality_cli import main
from dalton_core.research_quality_score import artefact_from_ask_answer
from tests.test_research_quality_cli import CliHarness

class AskCliTests(CliHarness):
    def setUp(self):
        super().setUp()
        self.journal=self.state/'held-cockpit.sqlite'
        self.result={'question':'合成公司收入增长是否持续？','answer':'当前申报提供季度收入证据，下一期需核查订单转化。',
                     'confidence':'medium','citations':[{'tag':'C1','ref':self.claim_ref,'statement':'Revenue rose 5.95%.','period':'2025-09-01..2025-11-30'}]}
        self.ref='cockpit-ask:synthetic-request'
        art=artefact_from_ask_answer(self.result,shown_claims=self.result['citations'],ref=self.ref)
        self.result['feedback']={'target_ref':self.ref,'target_hash':art['hash'],'target_kind':'ask_answer'}
        connection=sqlite3.connect(self.journal)
        connection.execute('CREATE TABLE cockpit_jobs(job_id,kind,status,request_json,result_json)')
        connection.execute('INSERT INTO cockpit_jobs VALUES(?,?,?,?,?)',('synthetic-job','ask','done',json.dumps({'request_id':'synthetic-request'}),json.dumps(self.result)))
        connection.commit();connection.close()

    def test_cli_scores_the_held_ask_artifact_without_deliverable_resolution(self):
        out=io.StringIO()
        with patch('dalton_core.research_quality_cli._resolve_deliverable',side_effect=AssertionError('Ask routed to deliverable')),contextlib.redirect_stdout(out):
            code=main(['score','--state-dir',str(self.state),'--rubric','ask_answer','--target',self.ref,'--ask-journal',str(self.journal),'--dry-run'])
        self.assertIn(code,(0,2))
        result=json.loads(out.getvalue());self.assertEqual(result['target_ref'],self.ref)
        self.assertEqual(result['target_hash'],self.result['feedback']['target_hash'])
        self.assertEqual(result['rubric_ref'],'rubric:ask-answer')
        self.assertIsNone(result['judge']);self.assertIsNone(result['recorded'])

    def test_missing_or_changed_held_answer_is_refused_instead_of_scoring_a_deliverable(self):
        changed=dict(self.result);changed['answer']='A changed answer.'
        connection=sqlite3.connect(self.journal);connection.execute('UPDATE cockpit_jobs SET result_json=?',(json.dumps(changed),));connection.commit();connection.close()
        for args in ([],['--ask-journal',str(self.journal)]):
            with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                code=main(['score','--state-dir',str(self.state),'--rubric','ask_answer','--target',self.ref,'--dry-run',*args])
            self.assertEqual(code,1)
