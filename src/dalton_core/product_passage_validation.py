"""Recover rendered passage whitespace only from the exact acquired original.

Pack renderers trim boundary whitespace. A rendered row is not authority for
its original span: only the bounded original reader can supply those bytes.
"""
from pathlib import Path
import hashlib
from .evidence_index import parse_passage_ref


def rendered_passage_matches(original: str, rendered: str, render_header: str | None = None) -> bool:
    return isinstance(original,str) and isinstance(rendered,str) and (
        rendered == original or rendered == original.strip() or
        (isinstance(render_header,str) and rendered == original.strip()+'\n'+render_header))


def checked_rendered_passages(state_dir, sections, pack_passages):
    """Resolve actual citations; preserve the existing exact-hash pack contract.

    Only a boundary-trimmed row needs the stronger acquired-original proof.
    No network, index rebuild, or current replacement document is used.
    """
    used = {str(n['passage_ref']) for s in sections for n in s.get('numbers', [])
            if n.get('passage_ref')}
    result = {ref: pack_passages[ref] for ref in used if ref in pack_passages}
    invalid = set()
    for ref, text in result.items():
        parts = parse_passage_ref(ref)
        if hashlib.sha256(text.encode()).hexdigest()[:12] != parts['hash12']:
            invalid.add(ref)
    if not invalid:
        return result
    if str(state_dir) == ':memory:':
        raise ValueError('rendered passage requires its acquired original')
    from .initial_screen_failure_feedback import _checked_originals
    originals = _checked_originals(Path(state_dir), invalid,include_render_metadata=True)
    for ref in invalid:
        original = originals[ref]['text']
        if not rendered_passage_matches(original, result[ref],originals[ref].get('render_header')):
            raise ValueError('frozen rendered passage differs inside its exact original span')
        result[ref] = original
    return result
