from __future__ import annotations
import http.client,json,os,secrets,subprocess,tempfile,threading,unittest
from dataclasses import replace,asdict
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from types import MethodType
from dalton_core.agenda_control import AgendaControlApplication,AgendaControlConfig,_handler,_subject_for_login
from dalton_core.public_sharing_gateway import Gateway,GatewayConfig,Target,handler
from dalton_core.shared_access_identity import SharedLogin,shared_actor
from dalton_core.shared_public_urls import html_urls
from dalton_core.store import content_hash

ORIGIN='https://example.ts.net'
class SharingNetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.share=secrets.token_urlsafe(48);self.backend=secrets.token_urlsafe(48);self.servers=[];self.threads=[];self.calls=[]
        for name,value in [('share',self.share),('backend',self.backend)]:
            p=self.root/name;p.write_text(value);p.chmod(0o600)
        self.cfg=GatewayConfig('127.0.0.1',18973,ORIGIN,self.root/'manager.json',self.root/'legacy.json',self.root/'share',self.root/'backend','public')
        self.targets=[];self.apps=[]
        for slug in ('legacy','synthetic-b'):
            config=AgendaControlConfig('127.0.0.1',1024,'example.ts.net',Path('/bin/false'),('owner@example.com',),self.root/slug/'writer.sock',self.root/slug/'tokens','endpoint:test',60,60,shared_gateway={'secret_file':str(self.root/'backend'),'public_origin':ORIGIN,'principal':'public'})
            app=AgendaControlApplication(config,None);app.review_plane=object();app.cockpit_view=lambda path,login,query:{'status':'ready','source_text':'/admin is research text','content_hash':'f'*64}
            def post(app,action,login,session,csrf,body):
                value=app._review_body(session,csrf,body);self.calls.append({'actor':_subject_for_login(login),'action':action,'value':value});return {'status':'saved','actor':_subject_for_login(login)}
            app.post_cockpit=MethodType(post,app)
            server=self.start(_handler(app));target=Target(slug,'Synthetic '+slug,server.server_port,'example.ts.net');self.targets.append(target);self.apps.append(app)
        self.gateway=Gateway(self.cfg,registry=lambda:list(self.targets));self.proxy=self.start(handler(self.gateway))
    def start(self,handler_class):
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_class);server.daemon_threads=True;t=threading.Thread(target=server.serve_forever,daemon=True);t.start();self.servers.append(server);self.threads.append(t);return server
    def tearDown(self):
        for s in reversed(self.servers):s.shutdown();s.server_close()
        for t in self.threads:t.join(2)
        self.temp.cleanup()
    def request(self,path,*,method='GET',data=None,cookies='',headers=None,server=None):
        conn=http.client.HTTPConnection('127.0.0.1',(server or self.proxy).server_port,timeout=5);h={'Host':'example.ts.net'}
        if cookies:h['Cookie']=cookies
        if data is not None:h.update({'Content-Type':'application/json','Origin':ORIGIN})
        h.update(headers or {});raw=None if data is None else json.dumps(data).encode();conn.request(method,path,body=raw,headers=h);r=conn.getresponse();body=r.read();answer=(r.status,dict(r.getheaders()),body);conn.close();return answer
    def bootstrap(self):
        status,h,body=self.request('/bootstrap',method='POST',data={'credential':self.share});self.assertEqual(status,200);return h['Set-Cookie'].split(';',1)[0]
    def manifest(self,slug,cookie):
        status,h,b=self.request('/w/'+slug+'/v2/data/manifest',cookies=cookie);self.assertEqual(status,200);return json.loads(b)['csrf_token'],h.get('Set-Cookie','').split(';',1)[0]
    def test_no_tailnet_identity_or_public_credential_can_impersonate_owner(self):
        status,_,_=self.request('/w/legacy/v2/data/manifest',headers={'Tailscale-User-Login':'owner@example.com','X-Dalton-Shared-Gateway':self.backend});self.assertEqual(status,401)
        status,_,_=self.request('/v2/data/manifest',server=self.servers[0],headers={'X-Dalton-Shared-Gateway':self.share,'X-Dalton-Public-Prefix':'/dalton/w/legacy/'});self.assertEqual(status,403)
        status,_,_=self.request('/v2/data/manifest',server=self.servers[0],headers={'Tailscale-User-Login':'shared-access:public'});self.assertEqual(status,403)
        self.assertFalse(self.calls)
    def test_bootstrap_requires_same_origin_and_rotation_invalidates_session(self):
        status,_,_=self.request('/bootstrap',method='POST',data={'credential':self.share},headers={'Origin':'https://foreign.example'});self.assertEqual(status,403)
        cookie=self.bootstrap();new=secrets.token_urlsafe(48);(self.root/'share').write_text(new)
        status,_,_=self.request('/w/legacy/',cookies=cookie);self.assertEqual(status,401)
        status,_,_=self.request('/bootstrap',method='POST',data={'credential':self.share});self.assertEqual(status,403)
    def test_two_workspace_tabs_have_distinct_cookie_paths_and_csrf(self):
        cookie=self.bootstrap();a,ca=self.manifest('legacy',cookie);b,cb=self.manifest('synthetic-b',cookie)
        self.assertNotEqual(a,b);self.assertNotEqual(ca.split('=')[0],cb.split('=')[0])
        status,h,_=self.request('/w/legacy/v2/data/manifest',cookies=cookie+'; '+ca);self.assertEqual(status,200)
        status,_,_=self.request('/w/synthetic-b/v1/cockpit/goal_publish',method='POST',data={'x':1},cookies=cookie+'; '+cb,headers={'X-Dalton-CSRF':a});self.assertEqual(status,403);self.assertFalse(self.calls)
        status,_,raw=self.request('/w/synthetic-b/v1/cockpit/goal_publish',method='POST',data={'x':1},cookies=cookie+'; '+cb,headers={'X-Dalton-CSRF':b,'Tailscale-User-Login':'owner@example.com','X-Dalton-Public-Prefix':'/dalton/w/legacy/','Authorization':'Bearer forged'})
        self.assertEqual(status,200);self.assertEqual(json.loads(raw)['status'],'saved');self.assertTrue(self.calls[-1]['actor'].startswith('human:shared-'));self.assertNotEqual(self.calls[-1]['actor'],_subject_for_login('owner@example.com'))
        # The real backend's session cookie is scoped to this workspace.
        status,h,_=self.request('/w/synthetic-b/',cookies=cookie);self.assertIn('Path=/dalton/w/synthetic-b/;',h['Set-Cookie']);self.assertIn('HttpOnly',h['Set-Cookie'])
    def test_existing_native_cookie_does_not_replace_shared_session_on_same_hostname(self):
        status,h,_=self.request('/v2/data/manifest',server=self.servers[0],headers={'Tailscale-User-Login':'owner@example.com'})
        self.assertEqual(status,200);native=h['Set-Cookie'].split(';',1)[0]
        cookie=self.bootstrap();csrf,shared=self.manifest('legacy',cookie+'; '+native)
        self.assertNotEqual(native.split('=')[0],shared.split('=')[0]);self.assertTrue(shared.split('=')[0].endswith('_shared'))
        # Browser cookie order: the longer shared path first, native root last.
        jar=cookie+'; '+shared+'; '+native
        again,held=self.manifest('legacy',jar)
        self.assertEqual(again,csrf);self.assertEqual(held,'')
        status,_,body=self.request('/w/legacy/v1/cockpit/goal_publish',method='POST',data={},cookies=jar,headers={'X-Dalton-CSRF':csrf})
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['actor'],shared_actor(SharedLogin('public')))
        status,_,body=self.request('/v2/data/manifest',server=self.servers[0],cookies=jar,headers={'Tailscale-User-Login':'owner@example.com'})
        self.assertEqual(status,200)

    def test_native_tailnet_stays_available_and_shared_origin_is_enforced_twice(self):
        status,_,_=self.request('/v2/data/manifest',server=self.servers[0],headers={'Tailscale-User-Login':'owner@example.com'});self.assertEqual(status,200)
        cookie=self.bootstrap();csrf,backend_cookie=self.manifest('legacy',cookie)
        status,_,_=self.request('/w/legacy/v1/cockpit/goal_publish',method='POST',data={},cookies=cookie+'; '+backend_cookie,headers={'Origin':'https://wrong.example','X-Dalton-CSRF':csrf});self.assertEqual(status,403);self.assertFalse(self.calls)
    def test_funnel_stripped_prefix_static_admin_and_research_bytes(self):
        cookie=self.bootstrap();status,_,body=self.request('/w/legacy/',cookies=cookie);self.assertEqual(status,200);self.assertIn(b'/dalton/w/legacy/v2/static/shell.js',body);self.assertIn(b'dalton-base',body)
        status,_,body=self.request('/w/legacy/admin',cookies=cookie);self.assertEqual(status,200);self.assertIn(b'/dalton/w/legacy/v2/static/admin/admin.js',body)
        status,_,body=self.request('/w/legacy/v2/static/shell.js',cookies=cookie);self.assertEqual(status,200);self.assertIn(b'createApi',body)
        status,_,body=self.request('/w/legacy/v1/cockpit/overview',cookies=cookie);self.assertEqual(status,200);self.assertEqual(json.loads(body)['source_text'],'/admin is research text');self.assertEqual(json.loads(body)['content_hash'],'f'*64)
        status,_,_=self.request('/dalton/w/legacy/',cookies=cookie);self.assertEqual(status,404) # Never double-strip a mount.
    def test_deleted_target_removed_and_new_active_target_discovered(self):
        cookie=self.bootstrap();self.targets.pop(0);status,_,_=self.request('/w/legacy/',cookies=cookie);self.assertEqual(status,404)
        status,_,_=self.request('/w/synthetic-b/',cookies=cookie);self.assertEqual(status,200)
        self.targets.append(Target('synthetic-c','New project',self.servers[1].server_port,'example.ts.net'))
        status,_,body=self.request('/',cookies=cookie);self.assertEqual(status,200);self.assertIn(b'/dalton/w/synthetic-c/',body)
    def test_host_encoded_traversal_and_unknown_workspace_rejected(self):
        cookie=self.bootstrap()
        for path in ('/w/legacy/%2e%2e/admin','/w/legacy//v2/data/manifest','/w/absent/'):
            status,_,_=self.request(path,cookies=cookie);self.assertEqual(status,404)
        status,_,_=self.request('/',cookies=cookie,headers={'Host':'foreign.example'});self.assertEqual(status,421)
    def test_only_navigation_dtos_are_projected_not_research_or_record_hashes(self):
        raw=json.dumps({'items':[{'slug':'synthetic-b','url':'https://example.ts.net:8795/','text':'Keep /admin verbatim'}]}).encode()
        actual=self.gateway.workspace_urls(raw,'/v1/cockpit/workspaces',self.targets);d=json.loads(actual);self.assertEqual(d['items'][0]['url'],'/dalton/w/synthetic-b/');self.assertEqual(d['items'][0]['text'],'Keep /admin verbatim')
        self.assertEqual(self.gateway.workspace_urls(raw,'/v1/cockpit/overview',self.targets),raw)
    def test_export_html_and_binary_are_not_shell_projections(self):
        from dalton_core.cockpit_v2.routes import V2Response
        cookie=self.bootstrap();app=self.apps[0];route=app.v2_router
        body=b'<html><head></head><body>Proof /admin <a href="/v1/evidence">source</a></body></html>'
        route.raw_gets['/v2/data/export']=lambda q,c:(200,'text/html; charset=utf-8',body)
        status,_,actual=self.request('/w/legacy/v2/data/export',cookies=cookie);self.assertEqual(status,200);self.assertEqual(actual,body)
        binary=b'PK\x03\x04 synthetic xlsx';route.raw_gets['/v2/data/export-xlsx']=lambda q,c:(200,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',binary)
        status,_,actual=self.request('/w/legacy/v2/data/export-xlsx',cookies=cookie);self.assertEqual(status,200);self.assertEqual(actual,binary)

    def test_paused_project_uses_existing_read_only_reader_not_its_stopped_service(self):
        cookie=self.bootstrap()
        self.targets.append(Target('paused-project','暂停研究',1,'example.ts.net','paused','uuid-paused'))
        seen=[]
        def reader(path,login,query):
            seen.append((path,query,login))
            return {'name':'暂停研究','products':[{'kind':'dossier','sections':[{'title':'原文','body':'<script>literal</script> /admin remains research'}]}]}
        self.apps[0].cockpit_view=reader
        status,_,body=self.request('/w/paused-project/',cookies=cookie)
        self.assertEqual(status,200);self.assertIn('已暂停 · 只读'.encode(),body);self.assertIn(b'&lt;script&gt;',body);self.assertNotIn(b'<script>literal',body)
        self.assertEqual(seen[0][0],'/v1/cockpit/workspace_research');self.assertEqual(seen[0][1]['slug'],'paused-project');self.assertIsInstance(seen[0][2],SharedLogin)
        status,_,_=self.request('/w/paused-project/v1/cockpit/goal_publish',method='POST',data={},cookies=cookie)
        self.assertEqual(status,403);self.assertFalse(self.calls)

    def test_location_is_strictly_projected_and_foreign_redirect_is_refused(self):
        cookie=self.bootstrap()
        class Redirect(BaseHTTPRequestHandler):
            location='/admin?tab=budget'
            def log_message(self,*args):pass
            def do_GET(self):
                self.send_response(302);self.send_header('Location',self.location);self.send_header('Content-Length','0');self.end_headers()
        server=self.start(Redirect);self.targets.append(Target('redirect','Redirect fixture',server.server_port,'example.ts.net'))
        status,h,_=self.request('/w/redirect/',cookies=cookie)
        self.assertEqual(status,302);self.assertEqual(h['Location'],'/dalton/w/redirect/admin?tab=budget')
        Redirect.location='https://foreign.example/auth'
        status,h,_=self.request('/w/redirect/',cookies=cookie)
        self.assertEqual(status,503);self.assertNotIn('Location',h)
        # The pure projection is also used for backend redirects; unknown hosts never escape.
        self.assertEqual(self.gateway.location('/admin?tab=budget',self.targets[0],self.targets),'/dalton/w/legacy/admin?tab=budget')
        self.assertEqual(self.gateway.location('https://example.ts.net:'+str(self.targets[1].port)+'/v2/',self.targets[0],self.targets),'/dalton/w/synthetic-b/v2/')
        for bad in ('https://external.example/auth','//foreign.example/admin','../admin','/%2e%2e/admin'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.gateway.location(bad,self.targets[0],self.targets)

    def test_real_workspace_navigation_dto_shape_and_job_result_keep_identity(self):
        from dalton_core.workspace_manager import _public
        record={'workspace_id':'00000000-0000-0000-0000-000000000001','slug':'synthetic-b','name':'真实 DTO 形状','url':'https://example.ts.net:8795/','lifecycle':'active','status':'running','owner_login':'owner@example.com'}
        self.targets[1]=replace(self.targets[1],workspace_id=record['workspace_id'])
        dto=_public(record,'legacy')
        body=json.dumps({'enabled':True,'items':[dto]}).encode();projected=json.loads(self.gateway.workspace_urls(body,'/v1/cockpit/workspaces',self.targets))['items'][0]
        self.assertEqual(projected['url'],'/dalton/w/synthetic-b/');self.assertEqual(projected['workspace_id'],record['workspace_id']);self.assertEqual(projected['slug'],record['slug'])
        job={'job_id':'job:test','kind':'workspace_create','result':{'status':'running','workspace':dict(dto)}}
        got=json.loads(self.gateway.workspace_urls(json.dumps(job).encode(),'/v1/cockpit/job',self.targets))
        self.assertEqual(got['result']['workspace']['url'],'/dalton/w/synthetic-b/');self.assertEqual(got['job_id'],job['job_id'])
        bad=dict(dto,workspace_id='foreign')
        with self.assertRaises(ValueError):self.gateway.workspace_urls(json.dumps({'items':[bad]}).encode(),'/v1/cockpit/workspaces',self.targets)

    def test_registry_reads_real_manifest_and_creation_journal_and_lifecycle(self):
        from dalton_core.workspace import workspace_manifest
        host=self.root/'fleet';release=self.root/'release';release.mkdir()
        manager={'host_root':str(host),'release_path':str(release),'release_ref':'release:sha256:'+'a'*64,'owner_login':'owner@example.com','tailscale_host':'example.ts.net','tailscale_executable':'/bin/false','launch_agents_dir':str(self.root/'agents'),'ports':[18791,18792],'legacy_workspace':{'workspace_id':'legacy','name':'主项目','url':'https://example.ts.net:8792/','status':'running'},'shared_gateway':self.apps[0].config.shared_gateway}
        self.cfg.manager_config.write_text(json.dumps(manager));self.cfg.manager_config.chmod(0o600)
        def service(path,config,manifest=None):
            path.parent.mkdir(parents=True,exist_ok=True)
            raw={'control':{'enabled':True,'config':json.loads(json.dumps(asdict(config),default=str))}}
            if manifest:raw['workspace']={'manifest_path':str(manifest)}
            path.write_text(json.dumps(raw));path.chmod(0o600)
        service(self.cfg.legacy_service_config,self.apps[0].config)
        requests=host/'creation-requests';requests.mkdir(parents=True)
        def add(slug,port,lifecycle):
            wire=workspace_manifest(host,slug,port,'release:sha256:'+'a'*64,release)
            wire.pop('content_hash');wire['lifecycle']=lifecycle;wire['content_hash']=content_hash(wire)
            manifest=Path(wire['workspace_root'])/'workspace.json';manifest.parent.mkdir(parents=True);manifest.write_text(json.dumps(wire))
            service(Path(wire['config_path']),replace(self.apps[0].config,port=port,writer_socket=Path(wire['writer_socket'])),manifest)
            (requests/(slug+'.json')).write_text(json.dumps({'workspace_id':wire['workspace_id'],'slug':slug,'name':'项目 '+slug,'status':'paused' if lifecycle=='paused' else 'running','lifecycle':lifecycle,'owner_login':'owner@example.com'}))
            return manifest
        manifest=add('project-a',18791,'active');add('project-b',18792,'paused')
        gateway=Gateway(self.cfg);rows=gateway.registry()
        self.assertEqual([(r.slug,r.lifecycle) for r in rows],[('legacy','active'),('project-a','active'),('project-b','paused')])
        wire=json.loads(manifest.read_text());wire.pop('content_hash');wire['lifecycle']='deleted';wire['content_hash']=content_hash(wire);manifest.write_text(json.dumps(wire))
        self.assertEqual([r.slug for r in gateway.registry()],['legacy','project-b'])
        add('project-c',18793,'active');self.assertIn('project-c',[r.slug for r in gateway.registry()])
        pending=add('project-pending',18795,'active');pending_record=requests/'project-pending.json';record=json.loads(pending_record.read_text());record['status']='creating';pending_record.write_text(json.dumps(record));Path(json.loads(pending.read_text())['config_path']).unlink()
        self.assertNotIn('project-pending',[r.slug for r in gateway.registry()])
        path=host/'workspaces/project-c/config/service.json';bad=json.loads(path.read_text());bad['control']['config']['port']=18794;path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):gateway.registry()

class SharingIdentityTests(unittest.TestCase):
    def test_actor_is_shared_and_cli_requires_operator_owned_explicit_grant(self):
        from dalton_core.workspace_manager import _cli_login,_authorize,_is_admin
        config={'owner_login':'owner@example.com','shared_gateway':{'principal':'public'}}
        self.assertIsInstance(_cli_login(config,'shared-access:public'),SharedLogin);_authorize(config,SharedLogin('public'));self.assertTrue(_is_admin(config,SharedLogin('public')))
        with self.assertRaises(PermissionError):_authorize(config,'shared-access:public')
        with self.assertRaises(PermissionError):_cli_login(config,'shared-access:another')
        with self.assertRaises(PermissionError):_cli_login({'owner_login':'owner@example.com'},'shared-access:public')
        from dalton_core.cockpit_plane import _subject_for_login as cockpit_subject
        from dalton_core.research_review_control import _subject_for_login as review_subject
        for subject in (_subject_for_login,cockpit_subject,review_subject):self.assertEqual(subject(SharedLogin('public')),shared_actor(SharedLogin('public')))
    def test_shared_creation_inherits_control_grant_without_becoming_tailnet_owner(self):
        from tests.test_workspace_lifecycle import WorkspaceLifecycleTests
        from unittest.mock import patch
        from dalton_core.workspace_manager import create_managed_workspace,_write,list_workspaces
        fixture=WorkspaceLifecycleTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        binding={'secret_file':str(fixture.root/'backend-secret'),'public_origin':ORIGIN,'principal':'public'}
        fixture.config['shared_gateway']=binding;_write(fixture.config_path,fixture.config)
        def install(manifest,directory):
            from dalton_core.workspace import load_workspace_manifest
            from dalton_core.workspace_process import label_namespace
            ws=load_workspace_manifest(manifest)
            for role in ('writer','controller','control'):(Path(directory)/(label_namespace(ws.slug)+'.'+role+'.plist')).write_text('synthetic service')
        with patch('dalton_core.workspace_manager._readiness'),patch('dalton_core.workspace_manager._serve'),patch('dalton_core.workspace_process.install_workspace',side_effect=install),patch('dalton_core.workspace_manager.subprocess.run',fixture.run),patch('dalton_core.workspace_creation.sys.platform','test'):
            result=create_managed_workspace(fixture.config_path,SharedLogin('public'),'共享新项目','shared_create_001')
            replay=create_managed_workspace(fixture.config_path,SharedLogin('public'),'共享新项目','shared_create_001')
        self.assertEqual(result['workspace']['workspace_id'],replay['workspace']['workspace_id'])
        slug=result['workspace']['slug'];service=json.loads((fixture.host/'workspaces'/slug/'config/service.json').read_text())
        self.assertEqual(service['control']['config']['shared_gateway'],binding)
        self.assertEqual(service['control']['config']['allowed_tailscale_logins'],['owner@example.com'])
        self.assertNotIn('shared-access:public',service['control']['config']['allowed_tailscale_logins'])
        listing=list_workspaces(fixture.config_path,SharedLogin('public'),None)
        self.assertTrue(listing['is_admin']);self.assertEqual({x['slug'] for x in listing['items']},{fixture.slug,slug})

    def test_launchd_template_uses_installed_isolated_interpreter_and_no_secret_arguments(self):
        import plistlib
        template=(Path(__file__).parents[1]/'deploy/macos/launchagents/com.dalton.public-sharing-gateway.plist.template').read_text()
        rendered=template.replace('@@RELEASE_VENV@@','/isolated/venv').replace('@@GATEWAY_CONFIG@@','/private/config.json').replace('@@LOG_DIR@@','/private/log')
        value=plistlib.loads(rendered.encode())
        self.assertEqual(value['ProgramArguments'],['/isolated/venv/bin/python','-I','-m','dalton_core.public_sharing_gateway','--config','/private/config.json'])
        self.assertTrue(value['KeepAlive']);self.assertNotIn('EnvironmentVariables',value)

    def test_shared_feedback_keeps_real_writer_principal_and_distinct_human_subject(self):
        from tests.test_agenda_control import AgendaControlTests
        fixture=AgendaControlTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        app=AgendaControlApplication(fixture.config,fixture.plane)
        login=SharedLogin('public');_,session,_=app.session(login,None)
        with self.assertRaises(PermissionError):app.post(login,session,'wrong',b'{"decision_ref":"decision:control","verdict":"disagree"}')
        result=app.post(login,session,session.csrf,b'{"decision_ref":"decision:control","verdict":"disagree"}')
        self.assertEqual(result['verdict'],'disagree')
        row=fixture.store.connection.execute('SELECT subject_ref,source,actor_ref FROM agenda_feedback').fetchone()
        self.assertEqual(row['subject_ref'],shared_actor(login));self.assertEqual(row['actor_ref'],'bridge:tailscale-dashboard');self.assertEqual(row['source'],'shared_dashboard')

    def test_html_only_url_attributes_are_changed(self):
        body=b'<html><head></head><body><p>/admin is a research quote</p><a href="/admin">Admin</a><a href="https://external.example/doc">original</a></body></html>'
        got=html_urls(body,'/dalton/w/test/');self.assertIn(b'<p>/admin is a research quote</p>',got);self.assertIn(b'href="/dalton/w/test/admin"',got);self.assertIn(b'href="https://external.example/doc"',got)
    def test_browser_fetch_prefix_is_idempotent_and_leaves_external_urls_and_text_alone(self):
        js=Path(__file__).parents[1]/'src/dalton_core/cockpit_v2/public_path.js'
        code='''const fs=require('fs'),vm=require('vm'),assert=require('assert');let seen=[];const ctx={document:{querySelector:()=>({content:'/dalton/w/synthetic/'})},URL,Request,location:{origin:'https://example.ts.net'},fetch:(u)=>{seen.push(u);return Promise.resolve({})}};ctx.globalThis=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);ctx.fetch('/v1/cockpit/overview');ctx.fetch('/dalton/w/synthetic/v1/cockpit/overview');ctx.fetch('https://external.example/doc');assert.deepStrictEqual(seen,['/dalton/w/synthetic/v1/cockpit/overview','/dalton/w/synthetic/v1/cockpit/overview','https://external.example/doc']);assert.equal(ctx.DaltonPublicURL('/admin'),' /dalton/w/synthetic/admin'.trim());assert.equal(ctx.DaltonPublicURL('Research /admin'),'Research /admin');ctx.fetch(new URL('https://example.ts.net/v1/test?q=1'));assert.equal(seen.at(-1).href,'https://example.ts.net/dalton/w/synthetic/v1/test?q=1');const req=new Request('https://example.ts.net/v1/test',{method:'POST',headers:{'x-test':'kept'},body:'original'});ctx.fetch(req);assert.equal(seen.at(-1).url,'https://example.ts.net/dalton/w/synthetic/v1/test');assert.equal(seen.at(-1).method,'POST');assert.equal(seen.at(-1).headers.get('x-test'),'kept');assert.equal(req.bodyUsed,false);const ext=new URL('https://foreign.example/v1/test');ctx.fetch(ext);assert.equal(seen.at(-1),ext);'''
        subprocess.run(['node','-e',code,str(js)],check=True,capture_output=True)
