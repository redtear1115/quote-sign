from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class LineItem:
    name: str
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    subtotal: Optional[Decimal]


@dataclass(frozen=True)
class Quote:
    seller: Optional[str]
    buyer: Optional[str]
    quote_date: Optional[str]
    valid_until: Optional[str]
    delivery_date: Optional[str]
    items: list[LineItem]
    subtotal_total: Optional[Decimal]
    tax_rate: Optional[Decimal]
    tax: Optional[Decimal]
    total: Optional[Decimal]
    payment_terms: Optional[str]


@dataclass(frozen=True)
class ParsedPdf:
    markdown: str
    paragraphs: list[str]
    tables: list[list[list[str]]]
    page_count: int


@dataclass(frozen=True)
class LlmResult:
    data: dict
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    cost_is_estimate: bool
    seconds: float


@dataclass(frozen=True)
class Issue:
    layer: str
    code: str
    message: str
    row: Optional[int] = None
    field: Optional[str] = None


class PdfReader(Protocol):
    def parse(self, pdf_path: Path) -> ParsedPdf:
        ...


class LlmClient(Protocol):
    def chat_json(self, *, model: str, purpose: str, system: str, user: str,
                  max_tokens: int) -> LlmResult:
        ...
