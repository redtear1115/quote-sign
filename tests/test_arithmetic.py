from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from quote_sign.checks import check_arithmetic, quote_from_compdf
from quote_sign.money import fmt, parse_number


def test_correct_numbers(quote):
    assert check_arithmetic(quote) == []


def test_bad_second_subtotal(quote):
    items = list(quote.items)
    items[1] = replace(items[1], subtotal=Decimal(12000))
    issues = check_arithmetic(replace(quote, items=items))
    issue = next(issue for issue in issues if issue.code == "line_subtotal")
    assert issue.row == 2 and issue.field == "subtotal"
    assert "第 2 列小計應為 15,000" in issue.message
    assert "文件上是 12,000" in issue.message
    assert {issue.code for issue in issues} == {"line_subtotal", "subtotal_sum"}


@pytest.mark.parametrize("row", [1, 3])
def test_other_row_multiplication(quote, row):
    items = list(quote.items)
    items[row - 1] = replace(items[row - 1], quantity=Decimal(2))
    issues = check_arithmetic(replace(quote, items=items))
    assert len(issues) == 1
    assert issues[0].row == row and issues[0].code == "line_subtotal"


@pytest.mark.parametrize("field,value,code,text", [
    ("subtotal_total", "40000", "subtotal_sum", "小計加總應為 42,600"),
    ("tax", "2000", "tax", "營業稅應為 2,130"),
    ("total", "44000", "total", "含稅總計應為 44,730"),
])
def test_wrong_totals(quote, field, value, code, text):
    issues = check_arithmetic(replace(quote, **{field: Decimal(value)}))
    assert any(issue.code == code and text in issue.message for issue in issues)


@pytest.mark.parametrize("text,expected", [
    ("15,000", "15000"), ("１２,０００", "12000"), ("ＮＴ＄１２，０００", "12000"),
    (" NT$ 1,234.50 元 ", "1234.50"), ("5%", "5"), ("$1.25", "1.25"),
    ("-12.5", "-12.5"), (".5", ".5"), ("1\u3000234", "1234"),
])
def test_parse_numbers(text, expected):
    assert parse_number(text) == Decimal(expected)


@pytest.mark.parametrize("text", ["", "-", "abc", "12abc", "NaN", "Infinity", "1e3", None])
def test_unreadable_number(text):
    assert parse_number(text) is None


def test_format():
    assert fmt(Decimal("15000.00")) == "15,000"
    assert fmt(Decimal("1234.5")) == "1,234.50"


def test_blank_rows_not_counted(parsed):
    parsed.tables[0].insert(2, ["", " ", "", ""])
    parsed.tables[0][3][3] = "12,000"
    q, issues = quote_from_compdf(parsed)
    assert not issues
    assert len(q.items) == 3
    assert next(i for i in check_arithmetic(q) if i.code == "line_subtotal").row == 2


def test_decimal_arithmetic_and_half_up(quote):
    item = replace(quote.items[0], quantity=Decimal("1.5"), unit_price=Decimal("2"), subtotal=Decimal(3))
    q = replace(quote, items=[item], subtotal_total=Decimal(3), tax_rate=Decimal(50),
                tax=Decimal(2), total=Decimal(5))
    assert check_arithmetic(q) == []


def test_unreadable_cell(parsed):
    parsed.tables[0][2][1] = "?"
    q, issues = quote_from_compdf(parsed)
    assert q.items[1].quantity is None
    assert issues[0].code == "unreadable_cell"
    assert issues[0].row == 2 and issues[0].field == "quantity"
    assert any(i.code == "unreadable" and i.row == 2 for i in check_arithmetic(q))


def test_missing_totals(quote):
    fields = {field: None for field in ("subtotal_total", "tax_rate", "tax", "total")}
    issues = check_arithmetic(replace(quote, **fields))
    assert {i.field for i in issues if i.code == "missing_field"} == set(fields)


def test_no_items_fails_closed(quote):
    assert any(i.field == "items" for i in check_arithmetic(replace(quote, items=[])))
