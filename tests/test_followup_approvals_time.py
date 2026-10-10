"""Real automatic followups must remain renderable as Cockpit approval cards."""
import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.research_followup import ResearchFollowupAuthority,approval_items,requests
from dalton_core.research_followup_automation import FollowupAutomationAuthority,groups
from tests.test_cockpit_plane import CockpitHarness
from tests.test_research_followup_automation import ReflectionRoutingTests
class FollowupApprovalTimeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.h=CockpitHarness(self.root);self.addCleanup(self.h.close);self.c=self.h.h.h.core.connection
  self.c.execute('CREATE TABLE IF NOT EXISTS research_cycle_reflection_versions(version_id TEXT,record_json TEXT,content_hash TEXT)')
  self.auth=ResearchFollowupAuthority(self.h.h.h.core,clock=lambda:'2026-10-10T12:00:00+00:00')
  fixture=object.__new__(ReflectionRoutingTests);fixture.c=self.c;fixture.auth=self.auth;fixture.reflection()
 def test_full_approvals_read_has_real_time_without_formal_writes(self):
  before=list(self.c.execute('select record_json,content_hash from research_followup_requests'));total=self.c.total_changes
  result=self.h.plane.approvals();cards=[r for r in result['items'] if r['kind']=='research_followup']
  self.assertEqual(len(cards),4);self.assertTrue(all(r['at']=='2026-10-10T12:00:00+00:00' for r in cards));self.assertTrue(all(r['decision_required'] is False for r in cards));self.assertEqual(result['count'],0)
  self.assertEqual(before,list(self.c.execute('select record_json,content_hash from research_followup_requests')));self.assertEqual(total,self.c.total_changes)
 def test_later_automation_time_projects_without_changing_request_identity(self):
  before=list(self.c.execute('select record_json,content_hash from research_followup_requests'))
  auto=FollowupAutomationAuthority(self.h.h.h.core,clock=lambda:'2026-10-10T12:30:00+00:00');auto.observe_lane('dispatch_quality_scoring',{'status':'idle','pending':0})
  cards=approval_items(self.c,lambda ref:ref);scoring=next(r for r in cards if r['title']=='后台核对研究评分');self.assertEqual(scoring['at'],'2026-10-10T12:30:00+00:00');self.assertEqual(before,list(self.c.execute('select record_json,content_hash from research_followup_requests')))
  self.assertTrue(all('at' in r for r in self.h.plane.approvals()['items']))
 def test_authenticated_http_endpoint_keeps_background_cards_and_rejects_foreign_login(self):
  import http.client,json,threading
  from http.server import ThreadingHTTPServer
  from tests.test_agenda_control import AgendaControlTests,LOGIN
  from dalton_core.agenda_control import AgendaControlApplication,_handler
  other=AgendaControlTests('test_dashboard_feedback_uses_hashed_subject_and_csrf');other.setUp()
  server=ThreadingHTTPServer(('127.0.0.1',0),_handler(AgendaControlApplication(other.config,other.plane,cockpit_plane=self.h.plane)))
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
   headers={'Host':'dalton.example.ts.net','Tailscale-User-Login':LOGIN}
   conn.request('GET','/v1/cockpit/approvals',headers=headers);response=conn.getresponse();body=json.loads(response.read());self.assertEqual(response.status,200);self.assertEqual(len(body['items']),4);self.assertEqual(body['count'],0);self.assertTrue(all(row['at'] for row in body['items']))
   conn.request('GET','/v1/cockpit/approvals',headers={**headers,'Tailscale-User-Login':'foreign@example.com'});response=conn.getresponse();response.read();self.assertEqual(response.status,403);conn.close()
  finally:server.shutdown();server.server_close();thread.join();other.tearDown()
if __name__=='__main__':unittest.main()
