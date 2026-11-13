from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quote_sign.export_pdf import ExportResult
from quote_sign.models import LineItem, LlmResult, ParsedPdf, Quote
from quote_sign.pipeline import Deps

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        if not self.responses:
            raise AssertionError("Unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def fake_transport():
    return FakeTransport


@pytest.fixture
def sample_path():
    return ROOT / "sample" / "quote-Q2026-031.md"


@pytest.fixture
def parsed(sample_path):
    return ParsedPdf(
        markdown=sample_path.read_text(),
        paragraphs=[
            "賣方：南方之光工作室（範例資料）", "買方：晨星設計有限公司（範例資料）",
            "報價日期：2026-11-20", "有效期限：2026-12-05",
            "未稅合計：NT$42,600", "營業稅 5% : NT$2,130", "含稅總計：NT$44,730",
            "1. 付款條件：簽約後30日內一次付清。",
            "2. 交付日期：2026-12-15 前完成授權開通與主題交付。",
        ],
        tables=[[
            ["項目", "數量", "單價 (NT$)", "小計 (NT$)"],
            ["MarsDawn 團隊授權（年）", "12", "1,800", "21,600"],
            ["自訂主題設計", "1", "15,000", "15,000"],
            ["導入教學（2 小時）", "1", "6,000", "6,000"],
        ]], page_count=1, anchors={"buyer": (1, (87, 1165, 593, 1196)),
                                    "seller": (1, (86, 1226, 594, 1258))},
    )


@pytest.fixture
def quote():
    return Quote(
        seller="南方之光工作室(範例資料)", buyer="晨星設計有限公司(範例資料)",
        quote_date="2026-11-20", valid_until="2026-12-05", delivery_date="2026-12-15",
        items=[LineItem("MarsDawn 團隊授權（年）", Decimal(12), Decimal(1800), Decimal(21600)),
               LineItem("自訂主題設計", Decimal(1), Decimal(15000), Decimal(15000)),
               LineItem("導入教學（2 小時）", Decimal(1), Decimal(6000), Decimal(6000))],
        subtotal_total=Decimal(42600), tax_rate=Decimal(5), tax=Decimal(2130),
        total=Decimal(44730), payment_terms="簽約後30日內一次付清。",
    )


class FakeExport:
    def __init__(self):
        self.calls = []
        self.error = None
        self.warnings = []

    def __call__(self, md_path, out_dir):
        self.calls.append((md_path, out_dir))
        if self.error:
            raise self.error
        return ExportResult(out_dir / "from-export.pdf", 1, self.warnings)


class FakeReader:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []
        self.error = None

    def parse(self, path):
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.parsed


class FakeLlm:
    def __init__(self, quote):
        self.extraction = asdict(quote)
        self.review = {"verdict": "pass", "findings": []}
        self.calls = []
        self.error = None
        self.error_purpose = "抽欄位"

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.error and kwargs["purpose"] == self.error_purpose:
            raise self.error
        extraction = kwargs["purpose"] == "抽欄位"
        return LlmResult(self.extraction if extraction else self.review,
                         "actual-extractor" if extraction else "actual-reviewer",
                         100, 50, Decimal("0.001234"), extraction, 0.125)


class FakeSigner:
    def __init__(self):
        self.calls = []

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "fake-task"


@pytest.fixture
def deps(parsed, quote, tmp_path):
    return Deps(FakeExport(), FakeReader(parsed), FakeLlm(quote), FakeSigner(),
                "cheap", "strong", date(2026, 11, 20), tmp_path / "out")


@pytest.fixture(autouse=True)
def offline_and_private(monkeypatch, tmp_path):
    """Every test uses an isolated home and cannot open real network sockets."""
    import socket
    from quote_sign import secrets
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in secrets.SECRET_NAMES + secrets.EMAIL_NAMES:
        monkeypatch.delenv(name, raising=False)
    def blocked(*args, **kwargs):
        raise AssertionError("Real network I/O is forbidden in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
