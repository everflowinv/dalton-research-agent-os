#!/usr/bin/env python3
"""Hermetic P9d-3b RPC/HTTP/browser rehearsal. Never opens live state or model sockets.

Requires the project package installed (editable or wheel); tests/ supplies
explicit fixture source data. --browser-executable uses an already installed
Chromium and playwright; it never downloads a browser. Output is owner-only.
"""
from __future__ import annotations
import argparse
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # fixture helpers only; do not shadow installed src package
import dalton_core
from dalton_core.agenda_control import AgendaControlApplication, AgendaControlConfig, _handler
from dalton_core.alphaengine_acquisition_launcher import AlphaEngineAcquisitionLauncher
from dalton_core.document_extraction import DocumentExtractionModelWorker, HermeticExtractionAdapter
from dalton_core.model_router import ModelRouter
from dalton_core.research_review_control import ResearchReviewControlConfig, ResearchReviewControlPlane, _subject_for_login
from dalton_core.store import canonical_json
from dalton_core.writer_client import WriterClient
from dalton_core.writer_protocol import RemoteAuthorizationError, RemoteError
from dalton_core.writer_server import WriterServer, Principal, HUMAN_GOVERNANCE_OPERATIONS
from tests.test_document_extraction import ExtractionHarness
from tests.test_mission_source_discovery import plan_for_tests
from tests.test_transcript_polish_model_worker import policy, profile

LOGIN = 'fixture-owner@example.com'


def counts(path):
    with sqlite3.connect(path) as connection:
        result = {table: connection.execute('SELECT count(*) FROM '+table).fetchone()[0]
                  for table in ('claim_versions','evidence_versions','connector_invocations',
                                'transcript_correction_set_versions','transcript_claim_citation_bindings')}
        result['integrity'] = connection.execute('PRAGMA integrity_check').fetchone()[0]
        return result


class OtherPanes:
    """Empty unrelated panes, not research fixtures or writable authority."""
    def view(self, login):
        return {'as_of':datetime.now(timezone.utc).isoformat(),'items':[]}
    def answer_view(self):
        return {'subjects':[]}


def run(output: Path, browser_executable: str | None):
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {'fixture_only':True,'external_network_calls':0,'paid_model_calls':0,'live_writes':0,
              'package_path':dalton_core.__file__}
    with tempfile.TemporaryDirectory(prefix='d-p9d3b-',dir='/tmp') as name:
        root=Path(name)
        fixture=ExtractionHarness(root)
        context=fixture.context(); fixture.enable_fixture()
        output_wire=json.loads(fixture.adapter.output_text)
        review=fixture.review; mission=fixture.mission
        fixture.close()
        (root/'plan.json').write_text(canonical_json(plan_for_tests()))
        actor=_subject_for_login(LOGIN)
        principals={
            'fixture-human':Principal('fixture-human','fixture-only-human',HUMAN_GOVERNANCE_OPERATIONS,actor_ref=actor),
            'fixture-auto':Principal('fixture-auto','fixture-only-auto',HUMAN_GOVERNANCE_OPERATIONS,actor_ref='automation:coverage-mission'),
        }
        launcher=AlphaEngineAcquisitionLauncher(state_dir=root,governance_path=root/'unused-governance.json')
        writer=WriterServer(root/'core.sqlite',root/'writer.sock',principals,
                            acquisition_launcher=launcher,transcript_spool_dir=root/'spool',
                            candidate_staging_path=root/'staging.sqlite',scheduler_path=root/'scheduler.sqlite',
                            discovery_plan_path=root/'plan.json')
        calls={'count':0}
        def factory(service, context, actor_ref):
            router=ModelRouter(root/'router.sqlite')
            adapter=HermeticExtractionAdapter(output_wire,created_at=datetime.now(timezone.utc).isoformat())
            worker=DocumentExtractionModelWorker(
                scheduler=writer._scheduler,router=router,store=writer.store,observability=writer.observability,
                adapter=adapter,routing_policy_ref=policy()['policy_version_ref'],
                credential_slot_refs=[profile()['credential_slot_ref']],
                context_resolver=lambda c:service.reread(c,actor_ref))
            # Keep the service's exact worker type; count calls and close the
            # test router in run_once, without replacing the fixture adapter.
            original=worker.run_once
            def run_once(work):
                try:
                    result=original(work); calls['count']+=adapter.calls; return result
                finally: router.close()
            worker.run_once=run_once
            return worker
        writer.start(); thread=threading.Thread(target=writer.serve_forever,daemon=True); thread.start()
        client=WriterClient(str(root/'writer.sock'),'fixture-only-human',timeout=30)
        automatic=WriterClient(str(root/'writer.sock'),'fixture-only-auto',timeout=30)
        def governance(*args,actor_ref,operation,params):
            if actor_ref!=actor: raise AssertionError('HTTP identity not server-derived')
            return client.call(operation,params)
        review_plane=ResearchReviewControlPlane(
            ResearchReviewControlConfig(root/'staging.sqlite',root/'no-packets',60),
            writer_socket=root/'writer.sock',token_config=root/'unused.json',writer=client,governance_call=governance)
        config=AgendaControlConfig.from_mapping({
            'host':'127.0.0.1','port':8793,'tailscale_host':'fixture.ts.net','tailscale_executable':sys.executable,
            'allowed_tailscale_logins':[LOGIN],'writer_socket':str(root/'writer.sock'),'token_config':str(root/'unused.json'),
            'endpoint_ref':'openclaw:discord:fixture','feedback_timeout_seconds':86400,'sweep_interval_seconds':60})
        app=AgendaControlApplication(config,OtherPanes(),review_plane)
        server=ThreadingHTTPServer(('127.0.0.1',0),_handler(app))
        http_thread=threading.Thread(target=server.serve_forever,daemon=True); http_thread.start()
        before=counts(root/'core.sqlite')
        params={'review_id':review['review_id'],'expected_review_hash':context['review_hash'],'offset':0}
        try:
            view=client.call('mission_document_evidence',params)
            assert view['context']==context
            extract={**params,'expected_context_hash':context['content_hash']}
            assert client.call('generate_document_extraction',extract)['status']=='gated'
            for op,body in (('mission_document_evidence',params),('generate_document_extraction',extract)):
                try: automatic.call(op,body)
                except RemoteAuthorizationError: pass
                else: raise AssertionError('automation bypassed human gate')
            try: client.call('generate_document_extraction',{**extract,'expected_context_hash':'0'*64})
            except RemoteError: pass
            else: raise AssertionError('stale source was accepted')
            # HTTP uses the actual shared session/CSRF shell, not an intercepted route.
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=30)
            headers={'Host':'fixture.ts.net','Tailscale-User-Login':LOGIN}
            connection.request('GET','/v1/mission-document-review',headers=headers)
            response=connection.getresponse(); cookie=response.getheader('Set-Cookie').split(';',1)[0]
            queue=json.loads(response.read()); assert response.status==200
            csrf=queue['csrf_token']
            body=json.dumps({'review_id':review['review_id'],'review_hash':context['review_hash'],'offset':0})
            for suffix,extra in (('evidence',{}),('extract',{'context_hash':context['content_hash']})):
                post=json.dumps({**json.loads(body),**extra})
                for supplied,code in (('wrong',403),(csrf,200)):
                    connection.request('POST','/v1/mission-document-review/'+suffix,body=post,
                        headers={**headers,'Cookie':cookie,'Content-Type':'application/json','X-Dalton-CSRF':supplied})
                    response=connection.getresponse(); response.read(); assert response.status==code
            connection.request('POST','/v1/mission-document-review/stage',body='{}',
                headers={**headers,'Cookie':cookie,'Content-Type':'application/json','X-Dalton-CSRF':'wrong'})
            response=connection.getresponse();response.read();assert response.status==403
            report['rpc_http_gate_csrf_stale_automation_checks']=True
            writer._document_extraction_worker_factory=factory
            human_stages = 0
            if browser_executable:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser=p.chromium.launch(executable_path=browser_executable,headless=True)
                    browser_context=browser.new_context(viewport={'width':1280,'height':1000},
                                                        extra_http_headers={'Tailscale-User-Login':LOGIN})
                    page=browser_context.new_page(); errors=[]; external=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('request',lambda request:external.append(request.url) if not request.url.startswith(f'http://127.0.0.1:{server.server_port}/') else None)
                    page.goto(f'http://127.0.0.1:{server.server_port}/')
                    queue_el=page.locator('#document-review-list'); queue_el.locator('article').first.wait_for()
                    queue_el.get_by_role('button',name='查看原文与抽取建议').click()
                    queue_el.get_by_text('原文与来源校验通过',exact=True).wait_for()
                    assert queue_el.locator('.document-evidence img,.document-evidence script').count()==0
                    assert page.evaluate('window.XSS||null') is None
                    queue_el.get_by_role('button',name='下一段',exact=True).click()
                    queue_el.get_by_text('原文与来源校验通过',exact=True).wait_for()
                    assert queue_el.get_by_role('button',name='下一段',exact=True).is_disabled()
                    queue_el.get_by_role('button',name='上一段',exact=True).click()
                    queue_el.get_by_text('原文与来源校验通过',exact=True).wait_for()
                    queue_el.get_by_role('button',name='生成本段抽取建议').click()
                    queue_el.get_by_text('待核对的语义建议（非正式 Claim）',exact=True).wait_for()
                    assert queue_el.get_by_text('以下为零网络 fixture 演练输出，不是真实模型或真实研究结论。',exact=True).is_visible()
                    assert queue_el.get_by_role('button',name='登记抽取完成').is_disabled()
                    def stage_in_browser(label):
                        editor=queue_el.locator('.extraction-editor')
                        button=editor.get_by_role('button',name='确认引用并保存为待审候选')
                        assert button.is_disabled()
                        editor.get_by_label('校订后的语义陈述').fill('Fixture human reviewed cautious client decisions.')
                        editor.get_by_label('口径与归属').fill(label)
                        editor.get_by_label('引用终点',exact=True).fill('100')
                        editor.get_by_label('人工核对理由').fill('Fixture human verified exact original, attribution and uncertainty.')
                        editor.get_by_label('已核对原文引用及校订陈述').check()
                        button.click()
                        editor.get_by_text('已保存待审候选：',exact=False).wait_for()
                        assert button.is_disabled()
                        assert queue_el.get_by_role('button',name='登记抽取完成').is_disabled()
                        assert page.evaluate('window.XSS||null') is None
                    stage_in_browser('desktop fixture human review')
                    human_stages+=1
                    page.screenshot(path=str(output/'desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    queue_el.get_by_role('button',name='查看原文与抽取建议').click()
                    queue_el.get_by_text('待核对的语义建议（非正式 Claim）',exact=True).wait_for()
                    stage_in_browser('mobile fixture human review')
                    human_stages+=1
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(output/'mobile.png'),full_page=True)
                    page.reload(); queue_el.get_by_role('button',name='查看原文与抽取建议').click()
                    queue_el.get_by_text('待核对的语义建议（非正式 Claim）',exact=True).wait_for()
                    queue_el.get_by_role('button',name='生成本段抽取建议').click()
                    queue_el.get_by_text('建议已保存，仍需人工核对',exact=True).wait_for()
                    assert calls['count']==1
                    # Resolve outside the stale browser; its next evidence request must fail.
                    client.call('resolve_mission_document_review',{'review_id':review['review_id'],
                        'expected_review_hash':context['review_hash'],'resolution':'dismissed','rationale':'Fixture only.'})
                    queue_el.get_by_role('button',name='查看原文与抽取建议').click()
                    queue_el.get_by_text('无法读取：HTTP 400。请刷新待审队列。',exact=True).wait_for()
                    assert queue_el.locator('.document-evidence pre').count()==0
                    assert page.locator('#agenda-list .empty').count()==1
                    assert errors==[] and external==[],(errors,external)
                    report['browser']={'desktop':1280,'mobile':390,'overflow':False,'xss':False,
                        'pagination':True,'reload_replay':True,'stale_failure_clears_source':True,
                        'page_errors':errors,'external_requests':external,'human_citation_staging_desktop_and_mobile':True}
                    browser.close()
            else:
                result=client.call('generate_document_extraction',extract)
                assert result['status']=='succeeded'
                assert client.call('generate_document_extraction',extract)==result
                assert calls['count']==1
                suggestion=result['suggestions'][0];q=suggestion['citation']
                stage={**extract,'suggestion_ref':suggestion['id'],'suggestion_hash':suggestion['content_hash'],
                    'request_id':'rpc-human-stage','normalized_statement':'Fixture human reviewed cautious client decisions.',
                    'metric_or_aspect':suggestion['metric_or_aspect'],'period':suggestion['period'],'basis':suggestion['basis'],
                    'source_start':q['source_start'],'source_end':100,'raw_text':q['raw_text'][:100],
                    'rationale':'Fixture human verified exact raw citation.','confirm_citation':True,
                    'correction_set_version_ref':None,'correction_set_version_hash':None}
                try:automatic.call('stage_document_extraction',stage)
                except RemoteAuthorizationError:pass
                else:raise AssertionError('automation staged human citation')
                staged=client.call('stage_document_extraction',stage)
                assert staged['status']=='staged' and staged['claim_accepted'] is False
                assert client.call('stage_document_extraction',stage)['write_status']=='duplicate'
                for changes in ({'rationale':'changed same request'},{'raw_text':'forged'},{'expected_context_hash':'0'*64}):
                    try:client.call('stage_document_extraction',{**stage,**changes})
                    except RemoteError:pass
                    else:raise AssertionError('changed/stale stage accepted')
                human_stages+=1
                report['human_citation_staging_rpc_replay']=True
            report['fixture_adapter_calls']=calls['count']
            report['before']=before; report['after']=counts(root/'core.sqlite')
            for table in ('claim_versions','evidence_versions','connector_invocations','integrity'):
                assert report['before'][table]==report['after'][table]
            for table in ('transcript_correction_set_versions','transcript_claim_citation_bindings'):
                assert report['after'][table]-report['before'][table]==human_stages
            with sqlite3.connect(root/'staging.sqlite') as connection:
                # Only staged candidates exist; no human acceptance was emitted.
                report['staging_integrity']=connection.execute('PRAGMA integrity_check').fetchone()[0]
            report['explicit_human_stages']=human_stages
            report['ok']=True
        finally:
            server.shutdown(); server.server_close(); http_thread.join(timeout=5)
            review_plane.close(); writer.stop(); thread.join(timeout=5)
    path=output/'result.json'; path.write_text(json.dumps(report,indent=2)+'\n'); path.chmod(0o600)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--browser-executable')
    args=parser.parse_args()
    run(args.output.resolve(),args.browser_executable)
