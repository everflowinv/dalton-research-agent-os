"""Closed CAS update for an installed research-publication worker gate."""
from __future__ import annotations

import hashlib, json, os, re, stat
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION="successor-research-publication-gate-transition-0.1"
TARGET="research-publication-worker-config.json"
HEX40=re.compile(r"[0-9a-f]{40}")
HEX64=re.compile(r"[0-9a-f]{64}")

class ResearchPublicationGateTransitionError(RuntimeError): pass
def need(v:Any,msg:str)->None:
    if not v: raise ResearchPublicationGateTransitionError(msg)
def sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def read_regular(path:Path)->bytes:
    fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    try:
        st=os.fstat(fd);need(stat.S_ISREG(st.st_mode),"worker config is not regular")
        data=b""
        while True:
            block=os.read(fd,1024*1024)
            if not block:break
            data+=block
        need(os.fstat(fd).st_mtime_ns==st.st_mtime_ns,"worker config changed while read")
        return data
    finally:os.close(fd)
def parse(data:bytes)->dict[str,Any]:
    try:value=json.loads(data)
    except Exception as exc:raise ResearchPublicationGateTransitionError("worker config is invalid JSON") from exc
    need(isinstance(value,dict) and value.get("schema_version")=="research-publication-worker-config:0.1","worker config identity differs")
    gate=value.get("publication_gate")
    need(isinstance(gate,dict) and set(gate)=={"release_pointer","runtime_pointer","expected_release_ref","expected_source_commit"},"publication gate shape differs")
    need(isinstance(gate["expected_release_ref"],str) and gate["expected_release_ref"],"release ref is invalid")
    need(HEX40.fullmatch(str(gate["expected_source_commit"])) is not None,"source commit is invalid")
    from scripts.successor_research_publication_transition import validate_worker_config_bytes
    try:
        return validate_worker_config_bytes(data,
            expected_release_ref=gate["expected_release_ref"])
    except Exception as exc:
        raise ResearchPublicationGateTransitionError("worker config closed shape differs") from exc
def artifact(packet:Path,path:Path)->dict[str,Any]:
    packet=packet.resolve();path=path.resolve();need(path.is_relative_to(packet),"artifact is outside packet")
    data=read_regular(path);return {"file":path.relative_to(packet).as_posix(),"sha256":sha(data),"size":len(data),"mode":path.stat().st_mode&0o7777}
def build_transition(*,packet_root:Path,before_path:Path,after_path:Path)->dict[str,Any]:
    before=read_regular(before_path);after=read_regular(after_path);bv=parse(before);av=parse(after)
    bg=bv["publication_gate"];ag=av["publication_gate"]
    expected={**bv,"publication_gate":{**bg,"expected_release_ref":ag["expected_release_ref"],"expected_source_commit":ag["expected_source_commit"]}}
    need(av==expected and before!=after,"only publication gate identity may change")
    return validate_transition({"schema_version":SCHEMA_VERSION,"kind":"cas_replace_publication_gate","target":TARGET,
      "before":artifact(packet_root,before_path),"after":artifact(packet_root,after_path),
      "predecessor":{"release_ref":bg["expected_release_ref"],"source_commit":bg["expected_source_commit"]},
      "successor":{"release_ref":ag["expected_release_ref"],"source_commit":ag["expected_source_commit"]}})
def validate_transition(v:Mapping[str,Any])->dict[str,Any]:
    need(isinstance(v,Mapping) and set(v)=={"schema_version","kind","target","before","after","predecessor","successor"},"gate transition shape differs")
    need(v.get("schema_version")==SCHEMA_VERSION and v.get("kind")=="cas_replace_publication_gate" and v.get("target")==TARGET,"gate transition identity differs")
    for side in ("before","after"):
        a=v[side];need(isinstance(a,Mapping) and set(a)=={"file","sha256","size","mode"},"gate artifact shape differs")
        need(isinstance(a["file"],str) and a["file"] and not Path(a["file"]).is_absolute() and ".." not in Path(a["file"]).parts,"gate artifact path is unsafe")
        need(HEX64.fullmatch(str(a["sha256"])) is not None and isinstance(a["size"],int) and 0<a["size"]<=16000000 and a["mode"]==0o600,"gate artifact identity differs")
    for side in ("predecessor","successor"):
        x=v[side];need(isinstance(x,Mapping) and set(x)=={"release_ref","source_commit"} and isinstance(x["release_ref"],str) and x["release_ref"] and HEX40.fullmatch(str(x["source_commit"])),"gate endpoint differs")
    need(v["predecessor"]!=v["successor"],"gate endpoint did not change")
    return dict(v)
def artifact_bytes(packet:Path,row:Mapping[str,Any])->bytes:
    root=packet.resolve();path=root/row["file"];need(path.is_relative_to(root),"artifact escaped packet")
    data=read_regular(path);need(len(data)==row["size"] and sha(data)==row["sha256"],"gate artifact bytes differ");return data
def expected_state(packet:Path,transition:Mapping[str,Any])->tuple[bytes,bytes]:
    t=validate_transition(transition);before=artifact_bytes(packet,t["before"]);after=artifact_bytes(packet,t["after"])
    bv=parse(before);av=parse(after);need(bv["publication_gate"]["expected_release_ref"]==t["predecessor"]["release_ref"] and bv["publication_gate"]["expected_source_commit"]==t["predecessor"]["source_commit"],"before gate endpoint differs")
    need(av["publication_gate"]["expected_release_ref"]==t["successor"]["release_ref"] and av["publication_gate"]["expected_source_commit"]==t["successor"]["source_commit"],"after gate endpoint differs")
    expected={**bv,"publication_gate":{**bv["publication_gate"],**{"expected_release_ref":t["successor"]["release_ref"],"expected_source_commit":t["successor"]["source_commit"]}}}
    need(av==expected,"worker config changed outside publication gate identity");return before,after
def replace_exact(target:Path,expected:bytes,replacement:bytes)->None:
    need(target.is_file() and not target.is_symlink()
         and stat.S_IMODE(target.stat().st_mode)==0o600
         and read_regular(target)==expected,"worker config CAS differs")
    temp=target.with_name("."+target.name+".gate-transition")
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,"wb") as f:f.write(replacement);f.flush();os.fsync(f.fileno())
        need(read_regular(target)==expected,"worker config changed before replace");os.replace(temp,target)
    finally:temp.unlink(missing_ok=True)
def apply(*,packet_root:Path,state_dir:Path,transition:Mapping[str,Any])->dict[str,Any]:
    before,after=expected_state(packet_root,transition);target=state_dir/TARGET;replace_exact(target,before,after)
    return {"before_sha256":sha(before),"after_sha256":sha(after),"target":TARGET,"configuration_mutations":1}
def rollback(*,packet_root:Path,state_dir:Path,transition:Mapping[str,Any])->dict[str,Any]:
    before,after=expected_state(packet_root,transition);target=state_dir/TARGET
    need(target.is_file() and not target.is_symlink()
         and stat.S_IMODE(target.stat().st_mode)==0o600,
         "worker config mode differs during rollback")
    current=read_regular(target)
    if current==before:return {"status":"already_rolled_back","target":TARGET}
    need(current==after,"worker config changed after gate transition");replace_exact(target,after,before)
    return {"status":"rolled_back","target":TARGET,"restored_sha256":sha(before)}
