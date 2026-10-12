"""Expose a refused historical calculation to the existing planner feedback.

No amount, source, formula, budget or retry permission is changed here.
"""
from .store import content_hash


def describe_failure(error):
    from .company_financial_statement_structure import FinancialStatementStructureError
    detail = getattr(error, 'diagnostics', None)
    if not isinstance(error, FinancialStatementStructureError) or not isinstance(detail, dict):
        return None, ''
    body = {k: v for k, v in detail.items() if k != 'content_hash'}
    if (detail.get('contract_ref') != 'model-history-tie-diagnostics:0.1'
            or detail.get('content_hash') != content_hash(body)
            or detail.get('financial_evidence') is not False
            or detail.get('retry_authority') is not False
            or not isinstance(detail.get('mismatches'), list) or not detail['mismatches']):
        return None, ''
    item = detail['mismatches'][0]
    if not isinstance(item, dict) or set(item) != {'period_start', 'period_end', 'calculated', 'filed'}:
        return None, ''
    # The lane's existing 500-character reason reaches the research planner.
    # Start with one exact dated discrepancy rather than truncating all of them.
    hint = (f"；首个差异期间 {item['period_start']} 至 {item['period_end']}，"
            f"公式结果 {item['calculated']}，申报值 {item['filed']}。"
            "需核对该期原始口径及公式分量；本次未接受计算结果。")
    return detail, hint
