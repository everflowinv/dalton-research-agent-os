"""Exclude product-name digits only when an exact cited source names the product.

This is a lexical proof, never a numerical source or arithmetic operand. The
caller supplies only passages already accepted by its normal source resolver.
"""
import re

RULE_REF = 'number-source-product-identifiers:1'
_IDENTIFIER = re.compile(r'(?<![A-Za-z0-9_$€£¥])([A-Za-z]{1,12}[0-9]{1,8})(?![A-Za-z0-9_%])')
_PRODUCT = r'(?:mainframe|processor|chip|server|router|handset|device|platform|product|model)'
_NAMED = re.compile(r'(?<![A-Za-z0-9_])(?:'
    r'(?P<after>[A-Za-z]{1,12}[0-9]{1,8})\s+' + _PRODUCT + r'\b|'
    + _PRODUCT + r'\s+(?P<before>[A-Za-z]{1,12}[0-9]{1,8})(?![A-Za-z0-9_]))', re.IGNORECASE)
# Period labels and chemical/physical metrics retain their numerical meaning.
# A source saying "FY2027 model" or "CO2 device" is not a product-name proof.
_RESERVED = frozenset({'FY', 'CY', 'Q', 'H', 'Y', 'CO', 'NO', 'SO', 'SCOPE'})


def without_verified_product_identifiers(text, passages):
    names = set()
    for source in passages:
        if not isinstance(source, str):
            continue
        for match in _NAMED.finditer(source):
            name = match.group('after') or match.group('before')
            prefix = re.match('[A-Za-z]+', name).group().upper()
            if prefix not in _RESERVED:
                names.add(name)  # exact spelling; no label translation or case inference
    return _IDENTIFIER.sub(lambda match: ' ' * len(match.group())
                           if match.group() in names else match.group(), text)
