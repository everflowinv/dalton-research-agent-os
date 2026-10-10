"""Transport-only URL prefixing: attributes, never research text or record hashes."""
from __future__ import annotations
from html.parser import HTMLParser
from html import escape,unescape
import re

LOCAL_PREFIXES=('/v1/','/v2/','/admin','/legacy','/favicon.ico')
def local_url(value,prefix):
    if value=='/' or value.startswith(('/#','/?')):return prefix+value[1:]
    if re.match(r'^/(v1/|v2/|admin(?:[/?#]|$)|legacy(?:[/?#]|$)|favicon\.ico(?:[?#]|$))',value):return prefix+value[1:]
    return value

class _Attributes(HTMLParser):
    def __init__(self,prefix):
        super().__init__(convert_charrefs=False);self.prefix=prefix;self.parts=[]
    def handle_starttag(self,tag,attrs):
        raw=self.get_starttag_text()
        def changed(m):
            url=unescape(m.group(3));new=local_url(url,self.prefix)
            return m.group(0) if new==url else m.group(1)+m.group(2)+escape(new,quote=True)+m.group(2)
        raw=re.sub(r'((?:\bhref|\bsrc|\baction)\s*=\s*)([\"\'])(.*?)\2',changed,raw,flags=re.I|re.S)
        self.parts.append(raw)
        if tag.lower()=='head':self.parts.append('<meta name="dalton-base" content="'+escape(self.prefix,quote=True)+'"><script src="'+escape(self.prefix,quote=True)+'v2/static/public_path.js"></script>')
    def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs)
    def handle_endtag(self,tag):self.parts.append('</'+tag+'>')
    def handle_data(self,data):self.parts.append(data)
    def handle_comment(self,data):self.parts.append('<!--'+data+'-->')
    def handle_decl(self,data):self.parts.append('<!'+data+'>')
    def handle_entityref(self,name):self.parts.append('&'+name+';')
    def handle_charref(self,name):self.parts.append('&#'+name+';')
    def handle_pi(self,data):self.parts.append('<?'+data+'>')
def html_urls(body,prefix):
    p=_Attributes(prefix);p.feed(body.decode('utf-8'));p.close();return ''.join(p.parts).encode('utf-8')
