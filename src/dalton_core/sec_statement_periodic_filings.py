"""Bound a disclosure window by reports, preserving exact amended identities."""
from __future__ import annotations


def model_window(filings, limit):
    # Old non-amended states retain the exact old order and byte projection.
    if not any(f.get('form') in {'10-Q/A', '10-K/A'} for f in filings):
        return sorted(filings, key=lambda f:(f['report_date'],f['accession']))[-limit:]
    reports = {}
    for filing in filings:
        form = filing['form']
        if form not in {'10-Q','10-K','10-Q/A','10-K/A'}:
            raise ValueError('unsupported periodic filing form in model window')
        key = (filing['report_date'], form.removesuffix('/A'))
        held = reports.get(key)
        if held is None or (filing['filed'],filing['accession']) > (held['filed'],held['accession']):
            reports[key] = filing
    return sorted(reports.values(), key=lambda f:(f['report_date'],f['accession']))[-limit:]
