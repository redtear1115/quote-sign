from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from quote_sign.checks import diff_quotes


def test_identical(quote):
    assert diff_quotes(quote, quote, "ComPDF", "模型") == []


def test_disagreement_lists_both_values_without_mutation(quote):
    items = list(quote.items)
    items[1] = replace(items[1], subtotal=Decimal(12000))
    other = replace(quote, items=items)
    issues = diff_quotes(quote, other, "ComPDF", "模型")
    assert len(issues) == 1
    assert issues[0].message == "第 2 列小計：ComPDF 讀到 15,000，模型讀到 12,000"
    assert issues[0].row == 2 and issues[0].field == "subtotal"
    assert quote.items[1].subtotal == Decimal(15000)
    assert other.items[1].subtotal == Decimal(12000)


def test_item_count(quote):
    issues = diff_quotes(quote, replace(quote, items=quote.items[:1]), "ComPDF", "模型")
    assert len(issues) == 1 and issues[0].field == "items"
    assert "3" in issues[0].message and "1" in issues[0].message


def test_normalized_strings(quote):
    items = list(quote.items)
    items[0] = replace(items[0], name="ｍａｒｓｄａｗｎ團隊授權(年)")
    other = replace(quote, seller="南 方 之 光 工 作 室（範例資料）", items=items)
    assert diff_quotes(quote, other, "ComPDF", "模型") == []


@pytest.mark.parametrize("field", ["seller", "buyer", "quote_date", "valid_until", "delivery_date",
                                 "subtotal_total", "tax_rate", "tax", "total"])
def test_every_top_level_difference(quote, field):
    issues = diff_quotes(quote, replace(quote, **{field: None}), "甲", "乙")
    assert len(issues) == 1 and issues[0].field == field
    assert "未讀到" in issues[0].message


@pytest.mark.parametrize("field", ["name", "quantity", "unit_price", "subtotal"])
def test_every_item_difference(quote, field):
    items = list(quote.items)
    items[0] = replace(items[0], **{field: "其他" if field == "name" else None})
    issues = diff_quotes(quote, replace(quote, items=items), "甲", "乙")
    assert len(issues) == 1 and issues[0].row == 1 and issues[0].field == field


def test_both_absent(quote):
    q = replace(quote, seller=None, delivery_date=None, total=None)
    assert diff_quotes(q, q, "甲", "乙") == []
