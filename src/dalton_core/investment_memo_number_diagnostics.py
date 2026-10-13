"""Collect independent numeric defects without accepting or rewriting a section."""
from .mission_deliverable import (validate_section,validate_derived_number,
    MissionDeliverableError,MissionDeliverableConflict,MAX_SECTION_NUMBERS)


def findings(section, *, live_claim_refs, resolve_cell, resolve_passage, resolve_number):
    numbers=section.get('numbers') or []
    if not isinstance(numbers,list) or len(numbers)>MAX_SECTION_NUMBERS:
        return []  # The complete section validator supplies the shape refusal.
    trusted=[];pending=[];errors=[]
    def refuse(index,exc):
        errors.append({'section':section['title'],'code':'section_authority',
            'detail':('number['+str(index)+']: '+str(exc))[:700]})
    for index,item in enumerate(numbers):
        if isinstance(item,dict) and item.get('derivation') is not None:
            pending.append((index,item));continue
        try:
            # This is diagnostic-only. Each isolated item runs the existing
            # source validator; it never becomes a publishable section or
            # replaces validation of the original body and whole DAG below.
            value=validate_section({'title':section['title'],'body':'逐项数值来源核验。',
                'numbers':[item],'claim_refs':[],'gaps':[]},live_claim_refs=live_claim_refs,
                resolve_cell=resolve_cell,resolve_passage=resolve_passage,
                resolve_number=resolve_number,strict=True)
            trusted.extend(value['numbers'])
        except MissionDeliverableError as exc:
            refuse(index,exc)
    from .source_product_identifiers import without_verified_product_identifiers
    passages=[resolve_passage(n['passage_ref']) for n in trusted if n.get('passage_ref')]
    trusted=[{**n,'text':without_verified_product_identifiers(n['text'],[p for p in passages if p])} for n in trusted]
    proven=[]
    while pending:
        progress=False;waiting=[]
        for index,item in pending:
            try:
                proof=validate_derived_number(item,[*trusted,*({'text':p['derivation']['result']} for p in proven)],check_stray=False)
            except MissionDeliverableConflict as exc:
                if 'a derivation uses ' in str(exc):waiting.append((index,item,exc))
                else:refuse(index,exc)
                continue
            except MissionDeliverableError as exc:
                refuse(index,exc);continue
            proven.append(proof);progress=True
        if not progress:
            for index,_,exc in waiting:refuse(index,exc)
            break
        pending=[(index,item) for index,item,_ in waiting]
    accepted=[*trusted,*({'text':p['derivation']['result']} for p in proven)]
    for index,item in enumerate(numbers):
        if isinstance(item,dict) and item.get('derivation') is not None and any(p['derivation']==item['derivation'] for p in proven):
            try:validate_derived_number(item,accepted)
            except MissionDeliverableError as exc:refuse(index,exc)
    return errors
