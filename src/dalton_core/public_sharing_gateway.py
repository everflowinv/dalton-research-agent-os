"""Public shared-link gateway. Loopback proxy, explicit identity, existing writers.

The public credential is accepted only in bootstrap JSON. It is never a query
parameter, upstream identity, log argument, research input or browser storage.
"""
from __future__ import annotations
import argparse,base64,hashlib,hmac,http.client,json,os,re,secrets,threading,time
from dataclasses import dataclass
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit,urlencode
from .shared_gateway_auth import secret
from .workspace import load_workspace_manifest
from .workspace_manager import _config as manager_config

SLUG=re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
MAX_REQUEST=24*1024*1024
MAX_RESPONSE=128*1024*1024
BASE='/dalton/'
COOKIE='dalton_shared_access'

@dataclass(frozen=True)
class GatewayConfig:
    host:str
    port:int
    public_origin:str
    manager_config:Path
    legacy_service_config:Path
    share_secret_file:Path
    backend_secret_file:Path
    principal:str
    session_seconds:int=86400
    @classmethod
    def load(cls,path):
        p=Path(path)
        if p.is_symlink() or p.stat().st_uid!=os.getuid() or p.stat().st_mode&0o077:raise ValueError('gateway config permissions invalid')
        return cls.from_mapping(json.loads(p.read_text()))
    @classmethod
    def from_mapping(cls,raw):
        required={'host','port','public_origin','manager_config','legacy_service_config','share_secret_file','backend_secret_file','principal'}
        if not isinstance(raw,dict) or set(raw)-required-{'session_seconds'} or not required<=raw.keys():raise ValueError('gateway config shape invalid')
        origin=urlsplit(raw['public_origin'])
        if raw['host']!='127.0.0.1' or type(raw['port'])!=int or not 1024<=raw['port']<=65535:raise ValueError('gateway must bind loopback')
        if origin.scheme!='https' or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment:raise ValueError('public HTTPS origin required')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}',raw['principal']):raise ValueError('principal invalid')
        values=dict(raw)
        for k in ('manager_config','legacy_service_config','share_secret_file','backend_secret_file'):
            values[k]=Path(raw[k])
            if not values[k].is_absolute():raise ValueError('gateway paths must be absolute')
        if type(values.get('session_seconds',86400))!=int or not 300<=values.get('session_seconds',86400)<=86400:raise ValueError('session lifetime invalid')
        return cls(**values)

@dataclass(frozen=True)
class Target:
    slug:str
    name:str
    port:int
    tailscale_host:str
    lifecycle:str="active"
    workspace_id:str|None=None
    @property
    def prefix(self):return BASE+'w/'+self.slug+'/'

class Gateway:
    def __init__(self,config,*,registry=None,clock=time.time):
        self.config=config;self.registry=registry or self._registry;self.clock=clock
        if secrets.compare_digest(secret(config.share_secret_file),secret(config.backend_secret_file)):
            raise ValueError('public and backend credentials must differ')
    def _target(self,slug,name,service_path,workspace=None):
        from .agenda_control import AgendaControlConfig
        control=AgendaControlConfig.from_service_file(service_path)
        binding=control.shared_gateway
        if not binding or binding!={'secret_file':str(self.config.backend_secret_file),'public_origin':self.config.public_origin,'principal':self.config.principal}:raise ValueError('control shared gateway binding differs')
        if workspace is not None:
            raw=json.loads(service_path.read_bytes())
            if control.port!=workspace.cockpit_port or str(control.writer_socket)!=str(workspace.writer_socket):raise ValueError('workspace target endpoint differs')
            if raw.get('workspace',{}).get('manifest_path')!=str(workspace.manifest_path):raise ValueError('workspace service manifest differs')
        return Target(slug,str(name or slug),control.port,control.tailscale_host,workspace.lifecycle if workspace else "active",workspace.workspace_id if workspace else "legacy")
    def _registry(self):
        cfg=manager_config(self.config.manager_config);root=Path(cfg['host_root']).resolve();rows=[]
        if cfg.get('shared_gateway')!={'secret_file':str(self.config.backend_secret_file),'public_origin':self.config.public_origin,'principal':self.config.principal}:raise ValueError('manager shared gateway binding differs')
        legacy=cfg.get('legacy_workspace')
        if legacy and legacy.get('lifecycle','active')=='active':
            if legacy.get('config_path') is not None and Path(legacy['config_path']).resolve()!=self.config.legacy_service_config.resolve():raise ValueError('legacy service target differs from registry')
            name=legacy.get('name')
            # The normal rename operation stores legacy presentation metadata
            # here; the manager entry is only the original installation name.
            try:
                display=json.loads((root/'legacy-display'/'display.json').read_bytes())
                if isinstance(display,dict) and isinstance(display.get('display_name'),str) and display['display_name'].strip():
                    name=display['display_name']
            except (OSError,ValueError):
                pass
            rows.append(self._target('legacy',name,self.config.legacy_service_config))
        seen={r.port for r in rows};slugs={r.slug for r in rows}
        for path in sorted((root/'workspaces').glob('*/workspace.json')):
            workspace=load_workspace_manifest(path)
            if workspace.lifecycle not in {'active','paused'}:continue
            # The host's workspaces directory may be a registered storage
            # symlink. Compare canonical roots, while rejecting a per-workspace
            # symlink that escapes that directory or aliases another slug.
            workspace_directory=(root/'workspaces').resolve()
            expected_root=(workspace_directory/workspace.slug).resolve()
            if (workspace.workspace_root!=expected_root or expected_root.parent!=workspace_directory
                    or expected_root.name!=workspace.slug):raise ValueError('workspace root differs from registry')
            if workspace.slug in slugs or workspace.cockpit_port in seen:raise ValueError('ambiguous workspace route')
            # Creation journal and manifest must agree on lifecycle/identity.
            records=[]
            for p in (root/'creation-requests').glob('*.json'):
                r=json.loads(p.read_bytes())
                if r.get('workspace_id')==workspace.workspace_id:records.append(r)
            if len(records)!=1 or records[0].get('slug')!=workspace.slug or records[0].get('lifecycle','active')!=workspace.lifecycle:continue
            if records[0].get('status')!=('paused' if workspace.lifecycle=='paused' else 'running'):continue
            rows.append(self._target(workspace.slug,records[0].get('name'),workspace.config_path,workspace));seen.add(workspace.cockpit_port);slugs.add(workspace.slug)
        return rows
    def location(self,value,target,rows):
        """Redirect only into explicitly registered routes; never trust a client host."""
        if any(ord(char)<32 or ord(char)==127 for char in value):raise ValueError('redirect contains control characters')
        parsed=urlsplit(value)
        if parsed.username or parsed.password or parsed.scheme not in {'','http','https'}:
            raise ValueError('redirect address invalid')
        if parsed.netloc:
            matches=[r for r in rows if parsed.netloc in {r.tailscale_host+':'+str(r.port),'127.0.0.1:'+str(r.port)} and r.lifecycle=='active']
            if parsed.netloc==target.tailscale_host:matches=[target]
            if len(matches)!=1:raise ValueError('redirect target is not a registered workspace')
            target=matches[0]
        elif not parsed.path.startswith('/') or parsed.path.startswith('//'):
            raise ValueError('redirect must name an absolute cockpit route')
        if '\\' in parsed.path or re.search(r'%2f|%5c|%2e',parsed.path,re.I) or any(x in {'.','..'} for x in parsed.path.split('/')):
            raise ValueError('redirect path invalid')
        return target.prefix+parsed.path.lstrip('/')+('?' +parsed.query if parsed.query else '')+('#'+parsed.fragment if parsed.fragment else '')
    def landing(self,rows):
        cards=''.join('<article><h2>'+escape(r.name)+'</h2><p>'+('已暂停 · 只读' if r.lifecycle=='paused' else '研究项目')+'</p><a class="continue" href="'+escape(r.prefix,quote=True)+'">'+('查看研究' if r.lifecycle=='paused' else '继续研究')+'</a></article>' for r in rows)
        return ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dalton 研究工作台</title><style>body{font:16px/1.6 system-ui,sans-serif;background:#f5f7fa;color:#172a3a;margin:0;padding:32px 20px}main{max-width:960px;margin:auto}h1{font-size:26px}.projects{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:18px}article{background:white;border:1px solid #dde4ea;border-radius:12px;padding:24px;min-width:0}h2{font-size:20px;overflow-wrap:anywhere;margin:0}p{color:#526575}.continue{display:inline-block;padding:9px 16px;border-radius:7px;background:#087d71;color:white;text-decoration:none}a:focus-visible{outline:3px solid #3f83d5;outline-offset:3px}</style><script>if(location.hash)history.replaceState(null,"",location.pathname);</script><main><h1>Dalton 研究工作台</h1><p>选择要查看或继续研究的项目</p><div class="projects">'+cards+'</div></main></html>').encode()
    def paused_body(self,target,value):
        if not isinstance(value,dict) or not isinstance(value.get('products'),list):raise ValueError('read-only research response invalid')
        parts=['<!doctype html><html lang="zh"><meta charset="utf-8"><title>已暂停研究项目</title><a href="/dalton/">返回项目列表</a><h1>'+escape(target.name)+'（已暂停 · 只读）</h1><p>研究服务已停止。这里仅显示保存的研究正文。</p>']
        for product in value['products']:
            parts.append('<article><h2>'+escape(str(product.get('display_label') or product.get('label') or product.get('kind') or '研究成果'))+'</h2>')
            for section in product.get('sections',[]):parts.append('<h3>'+escape(str(section.get('title') or ''))+'</h3><p style="white-space:pre-wrap;overflow-wrap:anywhere">'+escape(str(section.get('body') or ''))+'</p>')
            parts.append('</article>')
        return ''.join(parts).encode()
    def _credential(self):return secret(self.config.share_secret_file)
    def cookie(self):
        body=json.dumps({'nonce':secrets.token_urlsafe(32),'expires':int(self.clock())+self.config.session_seconds},sort_keys=True,separators=(',',':')).encode();payload=base64.urlsafe_b64encode(body).decode().rstrip('=')
        signature=hmac.new(self._credential().encode(),payload.encode(),hashlib.sha256).hexdigest();return payload+'.'+signature
    def authenticated(self,headers):
        try:
            cookies=SimpleCookie();cookies.load(headers.get('Cookie',''));value=cookies[COOKIE].value;payload,sig=value.split('.',1)
            if not hmac.compare_digest(sig,hmac.new(self._credential().encode(),payload.encode(),hashlib.sha256).hexdigest()):return False
            body=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
            return set(body)=={'nonce','expires'} and type(body['expires'])==int and self.clock()<body['expires']<=self.clock()+self.config.session_seconds
        except (KeyError,ValueError,TypeError,json.JSONDecodeError):return False
    def host_ok(self,headers):
        expected=urlsplit(self.config.public_origin).netloc
        return headers.get_all('Host',[])==[expected]
    def workspace_urls(self,body,path,rows):
        """Only non-authoritative workspace navigation DTOs are projected."""
        if path!='/v1/cockpit/workspaces' and path not in {'/v1/cockpit/job','/v1/cockpit/workspace_create','/v1/cockpit/workspace_retry'}:return body
        value=json.loads(body)
        if not isinstance(value,dict):raise ValueError('workspace navigation response invalid')
        if path=='/v1/cockpit/job' and value.get('kind') not in {'workspace_create','workspace_retry'}:return body
        byslug={r.slug:r for r in rows}
        def workspace(x):
            if not isinstance(x,dict):return
            target=byslug.get(x.get('slug') or ('legacy' if x.get('workspace_id')=='legacy' else ''))
            if target is not None and target.workspace_id is not None and x.get('workspace_id')!=target.workspace_id:raise ValueError('workspace navigation identity differs from registry')
            if target is not None and 'url' in x:x['url']=target.prefix if target.lifecycle=='active' else None
            elif 'url' in x:x['url']=None
        if path=='/v1/cockpit/workspaces':
            for item in value.get('items',[]):workspace(item)
        else:
            result=value.get('result') if isinstance(value.get('result'),dict) else {}
            for item in (value,value.get('workspace'),result.get('workspace'),result.get('result')):
                workspace(item)
        return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()

def handler(gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def setup(self):
            super().setup();self.connection.settimeout(130)
        def log_message(self,*args):pass
        def reply(self,status,body,ctype='application/json; charset=utf-8',cookie=None):
            self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
            if cookie:self.send_header('Set-Cookie',COOKIE+'='+cookie+'; Path=/dalton/; Secure; HttpOnly; SameSite=Strict')
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)
        def fail(self,status,message):self.reply(status,json.dumps({'display_message':message},ensure_ascii=False).encode())
        def body(self,limit=MAX_REQUEST):
            lengths=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit() or int(lengths[0])>limit:raise ValueError('request body size invalid')
            return self.rfile.read(int(lengths[0]))
        def dispatch(self):
            try:
                if not gateway.host_ok(self.headers):self.fail(421,'访问地址不正确。');return
                # Funnel --set-path strips the mount, verified against the public IP.
                incoming=urlsplit(self.path)
                path=incoming._replace(path=BASE+incoming.path.lstrip('/'))
                if incoming.fragment or not incoming.path.startswith('/') or incoming.path.startswith('//') or '\\' in self.path or re.search(r'%2f|%5c|%2e',path.path,re.I) or any(x in {'.','..'} for x in path.path.split('/')):self.fail(404,'没有这个入口。');return
                if self.command=='POST' and self.headers.get_all('Origin',[])!=[gateway.config.public_origin]:self.fail(403,'请求来源不正确，没有执行。');return
                if path.path==BASE+'bootstrap':
                    if self.command!='POST' or path.query or self.headers.get_content_type()!='application/json':self.fail(405,'请使用分享链接进入。');return
                    raw=self.body(1024)
                    value=json.loads(raw)
                    if not isinstance(value,dict) or set(value)!={'credential'} or not isinstance(value['credential'],str) or not hmac.compare_digest(value['credential'],gateway._credential()):self.fail(403,'分享链接已失效。');return
                    self.reply(200,b'{"status":"ready"}',cookie=gateway.cookie());return
                if path.path==BASE:
                    if self.command not in {'GET','HEAD'}:self.fail(405,'不支持这个操作。');return
                    if not gateway.authenticated(self.headers):
                        body='''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dalton 研究工作台</title><style>body{font:16px/1.6 system-ui,sans-serif;color:#172a3a;max-width:640px;padding:24px;margin:auto}h1{font-size:26px}</style></head><body><h1>Dalton 研究工作台</h1><p id="message">请打开完整分享链接进入研究工作台。</p><script>(async()=>{const v=new URLSearchParams(location.hash.slice(1));const credential=v.get("share");history.replaceState(null,"",location.pathname);if(!credential)return;try{const r=await fetch("/dalton/bootstrap",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({credential})});if(r.ok)location.replace("/dalton/");else document.getElementById("message").textContent="分享链接已失效，请联系分享者。";}catch(e){document.getElementById("message").textContent="连接暂时不可用，请重新打开分享链接。";}})();</script></body></html>''';self.reply(200,body.encode(),'text/html; charset=utf-8');return
                    rows=gateway.registry();self.reply(200,gateway.landing(rows),'text/html; charset=utf-8');return
                match=re.fullmatch(r'/dalton/w/([a-z0-9][a-z0-9-]{0,62})/(.*)',path.path)
                if not match:self.fail(404,'没有这个研究项目。');return
                if not gateway.authenticated(self.headers):self.fail(401,'请重新打开完整分享链接。');return
                rows=gateway.registry();target=next((x for x in rows if x.slug==match.group(1)),None)
                if target is None:self.fail(404,'研究项目已停用或不存在。');return
                paused=target if target.lifecycle=='paused' else None
                if paused:
                    if self.command not in {'GET','HEAD'} or match.group(2):self.fail(403,'已暂停项目仅能查看保存的研究正文。');return
                    target=next((r for r in rows if r.lifecycle=='active'),None)
                    if target is None:self.fail(503,'目前没有可用的只读研究服务。');return
                    upstream='/v1/cockpit/workspace_research?'+urlencode({'slug':paused.slug})
                else:upstream='/'+match.group(2)
                if '//' in upstream:self.fail(404,'访问路径不正确。');return
                if path.query and not paused:upstream+='?'+path.query
                headers={'Host':target.tailscale_host,'X-Dalton-Shared-Gateway':secret(gateway.config.backend_secret_file),'X-Dalton-Public-Prefix':target.prefix,'Connection':'close'}
                # No client identity, forwarding, authorization or gateway cookie is inherited.
                for name in ('Accept','Content-Type','X-Dalton-CSRF','Origin'):
                    vals=self.headers.get_all(name,[])
                    if len(vals)>1:self.fail(400,'请求头重复，没有执行。');return
                    if vals:headers[name]=vals[0]
                cookies=SimpleCookie();cookies.load(self.headers.get('Cookie',''))
                allowed=[m.OutputString() for k,m in cookies.items() if re.fullmatch(r'dalton_session_[0-9a-f]{16}_shared',k)]
                if allowed:headers['Cookie']='; '.join(allowed)
                payload=self.body() if self.command=='POST' else None
                conn=http.client.HTTPConnection('127.0.0.1',target.port,timeout=125)
                try:
                    conn.request(self.command,upstream,body=payload,headers=headers);response=conn.getresponse();body=response.read(MAX_RESPONSE+1)
                    if len(body)>MAX_RESPONSE:raise ValueError('response exceeds limit')
                    ctype=response.getheader('Content-Type','application/octet-stream')
                    if paused and response.status==200:
                        body=gateway.paused_body(paused,json.loads(body));ctype='text/html; charset=utf-8'
                    elif ctype.startswith('application/json'):body=gateway.workspace_urls(body,urlsplit(upstream).path,gateway.registry())
                    location=response.getheader('Location')
                    if location is not None:location=gateway.location(location,target,rows)
                    self.send_response(response.status)
                    if location is not None:self.send_header('Location',location)
                    self.send_header('Content-Type',ctype)
                    for name in ('Content-Disposition','Content-Security-Policy','Referrer-Policy','X-Frame-Options','X-Content-Type-Options'):
                        value=response.getheader(name)
                        if value is not None:self.send_header(name,value)
                    for name,value in response.getheaders():
                        if name.lower()=='set-cookie' and re.match(r'dalton_session_[0-9a-f]{16}_shared=',value) and 'Path='+target.prefix+';' in value:self.send_header(name,value)
                    self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(body)));self.end_headers()
                    if self.command!='HEAD':self.wfile.write(body)
                finally:conn.close()
            except (BrokenPipeError,ConnectionResetError):self.close_connection=True
            except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException):self.fail(503,'共享入口暂时不可用，请稍后再试。')
        def do_GET(self):self.dispatch()
        def do_HEAD(self):self.dispatch()
        def do_POST(self):self.dispatch()
    return Handler

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,required=True);args=parser.parse_args(argv);cfg=GatewayConfig.load(args.config);gateway=Gateway(cfg)
    server=ThreadingHTTPServer((cfg.host,cfg.port),handler(gateway));server.daemon_threads=True
    try:server.serve_forever()
    finally:server.server_close()
if __name__=='__main__':main()
