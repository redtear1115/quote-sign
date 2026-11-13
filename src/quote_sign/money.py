from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Optional


def parse_number(text: str) -> Optional[Decimal]:
    value = unicodedata.normalize("NFKC", str(text))
    value = re.sub(r"\s+", "", value)
    for token in ("NT$", "$", "元", "%", ","):
        value = value.replace(token, "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def fmt(d: Decimal) -> str:
    return format(d, ",.0f" if d == d.to_integral_value() else ",.2f")
