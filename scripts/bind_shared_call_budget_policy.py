#!/usr/bin/env python3
"""Plan or stage shared call-cost bindings and reusable runtime templates."""
from __future__ import annotations
import argparse, json, shutil, sqlite3, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT/'src'))
from dalton_core.shared_call_budget_policy import load_shared_call_budget_policy
from dalton_core.workspace_model_setup import EXPECTED_CONFIG_NAMES, export_runtime_template
from dalton_core.workspace_service_setup import export_service_template


def _copy_db(source: Path, target: Path):
    with sqlite3.connect(f'file:{source}?mode=ro',uri=True) as src, sqlite3.connect(target) as dst: src.backup(dst)

def prepare(source_state: Path, source_service: Path, policy: Path, stage: Path|None):
    load_shared_call_budget_policy(policy)
    files={p.name for p in source_state.glob('*model-config.json')}
    if files != EXPECTED_CONFIG_NAMES: raise SystemExit('source must contain exact model config set')
    plan={'status':'planned' if stage is None else 'staged','source_files':len(files),
          'shared_call_budget_policy_path':str(policy.resolve()),'live_modified':False}
    if stage is None: return plan
    if stage.exists(): raise SystemExit('stage directory must not exist')
    state=stage/'state'/'dalton-core'; config=stage/'config'; state.mkdir(parents=True); config.mkdir()
    for name in EXPECTED_CONFIG_NAMES:
        wire=json.loads((source_state/name).read_text()); wire['shared_call_budget_policy_path']=str(policy.resolve())
        wire['model_router_db']=str(state/'model-router.sqlite')
        wire['budget_db']=str(state/'thesis-impact-budget.sqlite')
        (state/name).write_text(json.dumps(wire,sort_keys=True,indent=2)+'\n')
    _copy_db(source_state/'model-router.sqlite',state/'model-router.sqlite')
    _copy_db(source_state/'thesis-impact-budget.sqlite',state/'thesis-impact-budget.sqlite')
    service=json.loads(source_service.read_text())
    service['core_db']=str(state/'core.sqlite')
    for name in ('document-research-config.json','mission-document-research-lane.json',
                 'p12a-dossier-policy-v1.json','p12e-industry-framework-policy-v1.json',
                 'research-language-policy.json','tracking-policy.json','model-catalog-sync.json'):
        source=source_state/name
        if source.is_file(): shutil.copy2(source,state/name)
    for section in ('bounded_planner','thesis_impact'):
        service[section]['config']['shared_call_budget_policy_path']=str(policy.resolve())
    staged_service=config/'service.json'; staged_service.write_text(json.dumps(service,sort_keys=True,indent=2)+'\n')
    model_out=stage/'model-runtime.json'; service_out=stage/'service-runtime.json'
    # Keep WAL owners open while the exporters use their strict read-only
    # connections; clean copied databases otherwise have no WAL/SHM sidecars.
    keep_router=sqlite3.connect(state/'model-router.sqlite')
    keep_budget=sqlite3.connect(state/'thesis-impact-budget.sqlite')
    try:
        keep_router.execute('pragma journal_mode=wal')
        keep_budget.execute('pragma journal_mode=wal')
        export_runtime_template(state,model_out); export_service_template(staged_service,service_out)
    finally:
        keep_router.close(); keep_budget.close()
    plan.update(model_template=str(model_out),service_template=str(service_out)); return plan

def main():
    p=argparse.ArgumentParser(); p.add_argument('--source-state',type=Path,required=True); p.add_argument('--source-service',type=Path,required=True); p.add_argument('--policy-path',type=Path,required=True); p.add_argument('--stage',type=Path)
    a=p.parse_args(); print(json.dumps(prepare(a.source_state.resolve(),a.source_service.resolve(),a.policy_path.resolve(),None if a.stage is None else a.stage.resolve()),sort_keys=True))
if __name__=='__main__': main()
