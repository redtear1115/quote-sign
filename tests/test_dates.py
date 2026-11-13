from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from quote_sign.checks import check_dates

TODAY = date(2026, 11, 20)


def test_valid_dates(quote):
    assert check_dates(quote, TODAY) == []
    assert check_dates(quote, date(2026, 12, 5)) == []


def test_expired(quote):
    issues = check_dates(quote, date(2026, 12, 6))
    assert issues[0].code == "expired"
    assert "報價已過有效期限" in issues[0].message


@pytest.mark.parametrize("field", ["seller", "buyer", "quote_date", "valid_until"])
@pytest.mark.parametrize("value", [None, "", " "])
def test_required_fields(quote, field, value):
    assert any(i.code == "missing_field" and i.field == field
               for i in check_dates(replace(quote, **{field: value}), TODAY))


@pytest.mark.parametrize("field,code", [("valid_until", "validity_before_quote"),
                                      ("delivery_date", "delivery_before_quote")])
def test_date_order(quote, field, code):
    assert any(i.code == code for i in check_dates(replace(quote, **{field: "2026-11-19"}), TODAY))


@pytest.mark.parametrize("field", ["quote_date", "valid_until", "delivery_date"])
@pytest.mark.parametrize("value", ["2026-02-30", "invalid", "20261120", "2026-1-01"])
def test_bad_iso_dates(quote, field, value):
    assert any(i.code == "bad_date" and i.field == field
               for i in check_dates(replace(quote, **{field: value}), TODAY))


def test_optional_delivery_date(quote):
    assert check_dates(replace(quote, delivery_date=None), TODAY) == []
