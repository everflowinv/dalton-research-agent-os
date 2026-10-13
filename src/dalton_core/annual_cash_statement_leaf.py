"""A direct annual cash amount stays annual; it never supplies quarter cells."""
from datetime import date
from decimal import Decimal, InvalidOperation


def validate_source(source, *, unit, concept):
    from .company_financial_statement_structure import FinancialStatementStructureError
    def refused(reason):
        raise FinancialStatementStructureError(f'annual cash leaf {concept}: {reason}')
    values = {}
    for cell in source.get('duration_facts') or []:
        try:
            start, end = str(cell['period_start']), str(cell['period_end'])
            days = (date.fromisoformat(end)-date.fromisoformat(start)).days+1
            amount = Decimal(str(cell['value']))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            refused('filed duration identity/value is invalid')
        if not 291 <= days <= 380:
            continue
        if not amount.is_finite() or str(cell.get('unit') or '').casefold()!=unit or not cell.get('source_accessions'):
            refused('annual amount/unit/original filing evidence differs')
        key=(start,end)
        if key in values and values[key]!=amount:
            refused('same annual period has conflicting filed values')
        values[key]=amount
    if not values:
        refused('no exact filed annual duration; obtain this annual cash fact, do not infer quarters')
