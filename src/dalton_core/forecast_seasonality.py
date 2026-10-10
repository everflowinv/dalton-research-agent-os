"""Deterministic quarterly seasonality, with trend removed before detection."""
from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Mapping, Sequence

RULE_YEAR_OVER_YEAR = "rule:year-over-year-growth:1"
RULE_SEASONAL_FACTOR = "rule:seasonal-quarter-factor:1"


def seasonal_profile(cells: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows = sorted((dict(c) for c in cells if c.get("period_end") and c.get("value") is not None),
                  key=lambda c: str(c["period_end"]))[-12:]
    if len(rows) < 8:
        return None
    rows = rows[-(len(rows) // 4 * 4):]
    if any(not 80 <= (date.fromisoformat(str(b["period_end"])) -
                      date.fromisoformat(str(a["period_end"]))).days <= 100
           for a, b in zip(rows, rows[1:])):
        return None
    values = [Decimal(str(c["value"])) for c in rows]
    if any(v == 0 for v in values) or len({v > 0 for v in values}) != 1:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        years = [values[i:i + 4] for i in range(0, len(values), 4)]
        totals = [sum(y) for y in years]
        yoy = sum((b / a - 1 for a, b in zip(totals, totals[1:])), Decimal(0)) / (len(totals) - 1)
        trend = (1 + yoy) ** (Decimal(1) / 4)
        adjusted = [v / trend ** i for i, v in enumerate(values)]
        distributions = []
        for i in range(0, len(values), 4):
            block = adjusted[i:i + 4]
            mean = sum(block) / 4
            distributions.append([v / mean for v in block])
        factors = [sum(d[i] for d in distributions) / len(distributions) for i in range(4)]
        strength = max(factors) - min(factors)
        instability = max(abs(d[i] - factors[i]) for d in distributions for i in range(4))
        return {"significant": strength >= Decimal("0.20") and instability <= Decimal("0.12"),
                "strength": strength, "instability": instability, "factors": factors,
                "year_over_year_growth": yoy, "rows": rows}


def year_ago(end: str, values: Mapping[str, Any]) -> str | None:
    candidates = [key for key in values if 350 <= (date.fromisoformat(end) -
                  date.fromisoformat(key)).days <= 380]
    return min(candidates, key=lambda key: abs((date.fromisoformat(end) -
               date.fromisoformat(key)).days - 365)) if candidates else None
