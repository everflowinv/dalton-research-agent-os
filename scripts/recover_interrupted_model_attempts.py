#!/usr/bin/env python3
"""Validate or execute one closed interrupted-model recovery plan."""
import argparse, hashlib, json, os
from pathlib import Path

from dalton_core.model_interruption_recovery import recover

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--plan",required=True,type=Path)
    parser.add_argument("--receipt",required=True,type=Path)
    parser.add_argument("--execute",action="store_true")
    args=parser.parse_args()
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
    fd=os.open(args.receipt,flags,0o600)
    parent_fd=os.open(args.receipt.parent,os.O_RDONLY)
    try:os.fsync(parent_fd)
    finally:os.close(parent_fd)
    try:
        try:
            plan_bytes=args.plan.read_bytes(); plan=json.loads(plan_bytes)
            result=recover(plan,execute=args.execute)
            result["plan_sha256"]=hashlib.sha256(plan_bytes).hexdigest()
        except Exception as exc:
            plan_bytes=locals().get("plan_bytes",b"")
            failure={"schema_version":"dalton-model-interruption-recovery-failure:0.1",
                     "executed":args.execute,"plan_sha256":hashlib.sha256(plan_bytes).hexdigest(),
                     "error_type":type(exc).__name__,"error":str(exc)}
            payload=(json.dumps(failure,sort_keys=True,separators=(",",":"))+"\n").encode()
            os.write(fd,payload); os.fsync(fd)
            raise
        payload=(json.dumps(result,sort_keys=True,separators=(",",":"))+"\n").encode()
        os.write(fd,payload); os.fsync(fd)
    finally: os.close(fd)
    print(json.dumps({"receipt":str(args.receipt),"sha256":hashlib.sha256(payload).hexdigest(),"executed":args.execute},sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
