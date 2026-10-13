"""Exact, occurrence-bound precision displays of an already proved derivation.

This module owns no source, model, fee or retry authority. The caller must first
prove the original arithmetic and every operand using its normal validator.
"""
from __future__ import annotations

import ast
import hashlib
import re
from fractions import Fraction

CONTRACT_REF = 'derived-number-display:exact-expression-same-unit-half-up:1'
MAX_NODES = 128
MAX_INTEGER_BITS = 8192
# Unit words are recognized only as an exact suffix, never converted. A new
# monetary magnitude or currency is a different display identity.
_UNIT_LABEL = r'(?:(?:百万|千万|十亿|万亿|万|千|百|亿)?(?:美元|欧元|英镑|人民币|日元|港元|股)|倍|(?:million|billion) (?:USD|EUR|GBP|JPY|CNY|HKD)|USD|EUR|GBP|JPY|CNY|HKD|shares)'
_SCALAR = (r'(?P<currency>[$€£¥]?)[ \t]*(?P<value>-?\d+(?:\.\d+)?)[ \t]*'
           r'(?P<percent>%?)[ \t]*(?P<label>' + _UNIT_LABEL + r')?')
_TOKEN = re.compile(r'(?<![A-Za-z\d.])' + _SCALAR + r'(?![\d.])')
_DECLARED = re.compile(
    r'(?:(?:按|保留)(?P<places>一|二|1|2)位小数(?:显示为|写为|为)|显示为)[ \t]*'
    + _SCALAR + r'(?![\d.])')



def _exact_value(expression):
    tree = ast.parse(expression, mode='eval')
    if len(list(ast.walk(tree))) > MAX_NODES:
        raise ValueError('derived display expression exceeds its node bound')

    def evaluate(node):
        if isinstance(node, ast.Expression):
            value = evaluate(node.body)
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float):
            literal = ast.get_source_segment(expression, node)
            if not re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)', literal or ''):
                raise ValueError('derived display requires exact plain scalar literals')
            value = Fraction(literal)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = evaluate(node.operand)
            if isinstance(node.op, ast.USub):
                value = -value
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add): value = left + right
            elif isinstance(node.op, ast.Sub): value = left - right
            elif isinstance(node.op, ast.Mult): value = left * right
            else: value = left / right
        else:
            raise ValueError('derived display requires bounded arithmetic only')
        if max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_INTEGER_BITS:
            raise ValueError('derived display expression exceeds its integer bound')
        return value

    return evaluate(tree)


def _half_up(value, places):
    scale = 10 ** places
    magnitude = abs(value) * scale
    rounded = (2 * magnitude.numerator + magnitude.denominator) // (2 * magnitude.denominator)
    digits = f'{rounded // scale}.{rounded % scale:0{places}d}'
    return ('-' if value < 0 else '') + digits


def prove_displays(text, *, expression, result, period=None, unit=None):
    """Return typed proofs of complete explicit phrases, never value exemptions.

    ``text`` must have already had only verified equation occurrences removed.
    No editing occurs. An independent, exact rational evaluation decides the
    displayed value, avoiding a second rounding of the model's rounded result.
    """
    declarations = list(_DECLARED.finditer(text))
    if not declarations:
        return []
    def complete(match):
        # Do not treat an unrecognized unit word, or a recognized unit prefix
        # within a longer word, as a unitless display.
        return not re.match(r'[A-Za-z\u3400-\u9fff]', text[match.end():])

    candidates = [match for match in _TOKEN.finditer(text)
                  if Fraction(match['value']) == Fraction(result)
                  and not any(item.start() <= match.start() < item.end() for item in declarations)]
    if any(not complete(match) for match in candidates):
        raise ValueError('derived display result anchor has an unsupported unit boundary')
    anchors = {
        (match['currency'], match['percent'], match['label'] or '')
        for match in candidates
    }
    if len(anchors) != 1:
        raise ValueError('derived display lacks one unambiguous same-unit result anchor')
    currency, percent, label = anchors.pop()
    exact = _exact_value(expression)
    proofs = []
    for match in declarations:
        if not complete(match):
            raise ValueError('derived display has an unsupported unit boundary')
        places = ({'一': 1, '二': 2, '1': 1, '2': 2}[match['places']]
                  if match['places'] else len(match['value'].split('.', 1)[-1]) if '.' in match['value'] else 0)
        if places not in (1, 2):
            raise ValueError('derived display must explicitly declare one or two decimal places')
        expected = _half_up(exact, places)
        if ((match['currency'], match['percent'], match['label'] or '') != (currency, percent, label)
                or match['value'] != expected):
            raise ValueError('derived display does not replay same-unit ROUND_HALF_UP')
        proofs.append({
            'contract_ref': CONTRACT_REF, 'expression_sha256': hashlib.sha256(expression.encode()).hexdigest(),
            'result': result, 'period': period, 'unit': unit,
            'exact_numerator': str(exact.numerator), 'exact_denominator': str(exact.denominator),
            'display_value': currency + expected + percent, 'unit_label': label,
            'decimal_places': places, 'rounding': 'ROUND_HALF_UP',
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'span': [match.start(), match.end()], 'phrase': match.group(),
        })
    return proofs


def without_proved_displays(text, proofs):
    """Remove only the exact complete phrase occurrences just proved."""
    result = text
    previous = len(text)
    for proof in reversed(proofs):
        start, end = proof['span']
        if (type(start) is not int or type(end) is not int
                or not 0 <= start < end <= previous
                or result[start:end] != proof['phrase']
                or hashlib.sha256(text.encode()).hexdigest() != proof['text_sha256']):
            raise ValueError('derived display occurrence binding differs')
        result = result[:start] + ' ' * (end - start) + result[end:]
        previous = start
    return result
