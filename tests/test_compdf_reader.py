from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from quote_sign.checks import quote_from_compdf, quote_from_llm_json
from quote_sign.models import ParsedPdf


def test_compdf_correct_quote(parsed, quote):
    actual, issues = quote_from_compdf(parsed)
    assert issues == []
    assert actual == quote


@pytest.mark.parametrize("header", ["品名", "品項", "項目"])
def test_column_mapping_and_full_width(parsed, header):
    parsed.tables[0] = [
        ["金額（NT$）", header, "單價", "數量"],
        ["１２，０００", "測試品項", "６，０００", "２"],
    ]
    q, issues = quote_from_compdf(parsed)
    assert not issues
    assert q.items[0].subtotal == Decimal(12000)
    assert q.items[0].quantity == Decimal(2)
    assert q.items[0].name == "測試品項"


def test_non_item_tables_are_skipped(parsed):
    parsed.tables.insert(0, [["其他", "表格"], ["A", "B"]])
    assert len(quote_from_compdf(parsed)[0].items) == 3


def test_missing_labels_and_table():
    q, issues = quote_from_compdf(ParsedPdf("", [], [], 0))
    assert q.seller is None and q.total is None and q.payment_terms is None
    assert q.items == [] and issues[0].code == "missing_table"


def test_short_row_marks_missing_cell(parsed):
    parsed.tables[0][1] = ["品項", "1"]
    q, issues = quote_from_compdf(parsed)
    assert q.items[0].subtotal is None
    assert {i.field for i in issues} == {"unit_price", "subtotal"}


def test_converter_tolerates_types():
    q = quote_from_llm_json({"seller": "甲", "tax": 0.1, "tax_rate": "5%",
                             "items": [{"name": "品項", "quantity": 1, "unit_price": "1,234.50",
                                        "subtotal": None}]})
    assert q.tax == Decimal("0.1")
    assert q.items[0].quantity == Decimal(1)
    assert q.items[0].unit_price == Decimal("1234.50")
    assert q.items[0].subtotal is None
    assert q.buyer is None


@pytest.mark.parametrize("data", [None, [], {}, {"items": None}, {"items": [None]}])
def test_converter_malformed_input(data):
    assert quote_from_llm_json(data).total is None


def test_missing_numeric_label(parsed):
    q, _ = quote_from_compdf(replace(parsed, paragraphs=["未稅合計：讀不到"]))
    assert q.subtotal_total is None and q.tax is None
