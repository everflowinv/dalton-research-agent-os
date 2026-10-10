"""Quote-bound verifier feedback and local patches, replayed without a model."""
import copy
import json
import re


def repair_indices(product, candidate, failure):
    findings=(failure.get('verifier') or {}).get('findings')
    if findings:
        indices=[]
        for finding in findings:
            if not isinstance(finding,dict) or set(finding)!={'section_index','source_quote','candidate_quote','code','reason'}:
                raise ValueError('publication_verifier_feedback_unanchored')
            index=finding['section_index']
            if type(index) is not int or not 0<=index<len(product['sections']):
                raise ValueError('publication_verifier_feedback_unanchored')
            source=str(product['sections'][index].get('body') or '')
            target=str(candidate[index].get('body') or '')
            sq,cq=finding['source_quote'],finding['candidate_quote']
            code=finding['code']
            if (code not in {'changed_fact','changed_number','new_fact','missing_fact'}
                    or not isinstance(sq,str) or not isinstance(cq,str)
                    or not isinstance(finding['reason'],str) or not finding['reason'].strip()
                    or (not sq and code!='new_fact') or (sq and source.count(sq)!=1)
                    or (not cq and code!='missing_fact') or (cq and target.count(cq)!=1)):
                raise ValueError('publication_verifier_feedback_unanchored')
            if code=='changed_number':
                from .research_localization import _number_differences
                missing,added=_number_differences((sq,),(cq,))
                if not missing and not added:raise ValueError('publication_changed_number_feedback_not_confirmed')
            indices.append(index)
        return sorted(set(indices))
    reason=str(failure.get('reason') or '')
    explicit=re.search(r'localized section (\d+)',reason)
    if explicit: return [int(explicit.group(1))]
    # The deterministic validator aggregates financial tokens without a section
    # label. Re-run it section by section to identify the failed units.
    from .research_localization import validate_localized_text
    indices=[]
    for index,(source,target) in enumerate(zip(product['sections'],candidate)):
        try: validate_localized_text(dict(product,sections=[source]),{'sections':[dict(target,index=0)]})
        except ValueError: indices.append(index)
    if not indices: raise ValueError('publication_repair_feedback_unanchored')
    return indices


def local_repair_prompt(product, candidate, failure, *, decisions=None):
    indices=repair_indices(product,candidate,failure)
    return '\n'.join((
        '只修复反馈锚定的章节中的被拒句子，其他章节和句子不改。不得新增事实、数字、来源或改变意思。',
        '仅返回 JSON：{"decisions":原建议逐项决定,"sections":[{"index":原章节序号,"title":"...","body":"...","gaps":[]}]}。sections 只能包含下面这些原章节序号。',
        '原建议决定：'+json.dumps(decisions or [],ensure_ascii=False),
        '可复核反馈：'+json.dumps(failure,ensure_ascii=False,sort_keys=True),
        '待修章节：'+json.dumps([{'index':i,'source':product['sections'][i],'candidate':candidate[i]} for i in indices],ensure_ascii=False,sort_keys=True),
    )),indices


def local_patch_quotes(candidate,failure,indices):
    findings=(failure.get('verifier') or {}).get('findings') or []
    if findings:
        return {index:[finding['candidate_quote'] for finding in findings if finding['section_index']==index] for index in indices}
    from .product_contract import sentences
    return {index:[sentence for sentence in sentences(candidate[index]['body']) if re.search(r'\d',sentence)]
                  or [candidate[index]['body']] for index in indices}


def expand_local_patch(output, *, candidate, indices, decisions, quotes=None):
    if not isinstance(output,dict) or set(output)!={'decisions','sections'}:
        raise ValueError('local repair output shape is invalid')
    rows=output['sections']
    if not isinstance(rows,list) or [row.get('index') for row in rows]!=indices:
        raise ValueError('local repair changed an unrequested section')
    sections=copy.deepcopy(candidate)
    for row in rows:
        index=row['index'];prior=candidate[index]
        if set(row)!={'index','title','body','gaps'} or row['title']!=prior['title'] or row['gaps']!=prior['gaps']:
            raise ValueError('local repair changed an unrequested field')
        if quotes is not None:
            anchors=quotes.get(index,quotes.get(str(index),[]))
            spans=sorted((prior['body'].index(quote),quote) for quote in anchors if quote)
            pieces=[];cursor=0
            for start,quote in spans:
                if start<cursor:raise ValueError('local repair quotes overlap')
                pieces.append(re.escape(prior['body'][cursor:start]));pieces.append('.*?');cursor=start+len(quote)
            pieces.append(re.escape(prior['body'][cursor:]))
            pattern=''.join(pieces) if spans else '.*?'+re.escape(prior['body'])+'.*?'
            if not re.fullmatch(pattern,row['body'],re.DOTALL):raise ValueError('local repair changed an unrequested sentence')
        sections[index]=row
    return {'decisions':copy.deepcopy(output['decisions']),'sections':sections}
