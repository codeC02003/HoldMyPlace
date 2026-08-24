"""Currency handling.

Every monetary value in this package is a Decimal quantized to cents. Floats
are not used for money: the simulation sums hundreds of thousands of small
per-stop costs, and float drift there would land squarely on the contribution
figure the whole analysis turns on.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENTS = Decimal("0.01")

ZERO = Decimal("0.00")


def money(value: object) -> Decimal:
    """Coerce a number to a cent-quantized Decimal."""
    if isinstance(value, Decimal):
        return value.quantize(CENTS, rounding=ROUND_HALF_UP)
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


def pct(value: float) -> Decimal:
    """Coerce a rate (0.11 for 11%) to a Decimal without cent-quantizing it."""
    return Decimal(str(value))


def fmt(value: Decimal) -> str:
    """Render money for the console: -$1.20, $418.00."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"
