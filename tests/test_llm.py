from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import stat

import pytest

from quote_sign.errors import BudgetExceeded, LlmError
from quote_sign.llm import MixRouteClient, PRICES, SpendLedger
from quote_sign.transport import HttpResponse, TransportError

KEY = "fake-mixroute-credential"
SYSTEM = "private system prompt"
USER = "private user prompt"
MODEL = "deepseek-v4-flash"


def response(content='{"ok":true}', usage=None, **overrides):
    payload = {"model": MODEL, "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1000, "completion_tokens": 500} if usage is None else usage}
    payload.update(overrides)
    return HttpResponse(200, json.dumps(payload).encode())


def call(client, **overrides):
    arguments = dict(model=MODEL, purpose="抽欄位", system=SYSTEM, user=USER, max_tokens=600)
    arguments.update(overrides)
    return client.chat_json(**arguments)


@pytest.fixture
def setup_client(tmp_path, fake_transport):
    def setup(*responses, cap="5", clock=None):
        ledger = SpendLedger(tmp_path / "state" / "spend.json", Decimal(cap))
        transport = fake_transport(*responses)
        options = {} if clock is None else {"clock": clock}
        client = MixRouteClient("https://mix.example/v1/", KEY, ledger, transport, **options)
        return client, ledger, transport
    return setup


def test_cost_estimate_and_request(setup_client):
    ticks = iter([2.0, 2.125])
    client, ledger, transport = setup_client(response(), clock=lambda: next(ticks))
    result = call(client)
    assert result.cost_usd == Decimal("0.00028") and result.cost_is_estimate
    assert result.prompt_tokens == 1000 and result.completion_tokens == 500
    assert result.data == {"ok": True} and result.seconds == 0.125
    method, url, headers, body, timeout = transport.calls[0]
    assert (method, url, timeout) == ("POST", "https://mix.example/v1/chat/completions", 180)
    header_names = set(headers)
    assert "Authorization" in header_names and "Content-Type" in header_names
    request = json.loads(body)
    assert request == {"model": MODEL, "messages": [
        {"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}], "max_tokens": 600}
    assert "temperature" not in request
    assert ledger.total() == result.cost_usd


def test_actual_model_and_cost(setup_client):
    client, ledger, _ = setup_client(response(model="reported-model", usage={
        "input_tokens": 1000, "output_tokens": 500, "cost": 0.012345}, cost=1))
    result = call(client)
    assert result.model == "reported-model"
    assert result.cost_usd == Decimal("0.012345") and not result.cost_is_estimate
    assert result.prompt_tokens == 1000 and result.completion_tokens == 500
    assert json.loads(ledger.path.read_text())["calls"][0]["model"] == "reported-model"


@pytest.mark.parametrize("usage, expected", [({}, (len(SYSTEM) + len(USER), 600)),
    ({"prompt_tokens": 10}, (10, 600)), ({"completion_tokens": 0}, (len(SYSTEM) + len(USER), 0)),
    ({"prompt_tokens": -1, "completion_tokens": "bad"}, (len(SYSTEM) + len(USER), 600))])
def test_missing_usage_estimated(setup_client, usage, expected):
    client, _, _ = setup_client(response(usage=usage))
    result = call(client)
    assert (result.prompt_tokens, result.completion_tokens) == expected
    assert result.cost_is_estimate
    assert result.cost_usd == (expected[0] * Decimal("0.14") + expected[1] * Decimal("0.28")) / 1_000_000


@pytest.mark.parametrize("usage,cost", [({}, 0), ({"cost": "not numeric"}, 0.00004)])
def test_top_level_numeric_cost(setup_client, usage, cost):
    client, _, _ = setup_client(response(usage=usage, cost=cost))
    result = call(client)
    assert result.cost_usd == Decimal(str(cost)) and not result.cost_is_estimate


def test_ledger_private_accumulation(setup_client):
    client, ledger, _ = setup_client(response(), response())
    assert ledger.total() == 0 and not ledger.path.exists()
    call(client)
    call(client, purpose="審條款")
    assert ledger.total() == Decimal("0.00056")
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700
    text = ledger.path.read_text()
    assert all(secret not in text for secret in (KEY, SYSTEM, USER))
    stored = json.loads(text)
    assert len(stored["calls"]) == 2
    for entry in stored["calls"]:
        assert set(entry) == {"ts", "model", "purpose", "prompt_tokens", "completion_tokens", "cost_usd", "estimate"}
        assert datetime.fromisoformat(entry["ts"]).utcoffset().total_seconds() == 0
    assert list(ledger.path.parent.iterdir()) == [ledger.path]


@pytest.mark.parametrize("total,cap", [("5", "5"), ("5.1", "5"), ("0", "0.000001")])
def test_budget_precheck(setup_client, total, cap):
    client, ledger, transport = setup_client(cap=cap)
    ledger.path.parent.mkdir(mode=0o700)
    ledger.path.write_text(json.dumps({"total_usd": total, "calls": []}))
    with pytest.raises(BudgetExceeded, match="累計花費已達上限"):
        call(client)
    assert not transport.calls


def test_worst_case_exact_boundary(setup_client):
    client, ledger, transport = setup_client(response())
    worst = ((len(SYSTEM) + len(USER)) * Decimal("0.14") + 600 * Decimal("0.28")) / 1_000_000
    ledger.cap_usd = worst
    call(client)
    assert len(transport.calls) == 1


def test_unknown_model_without_request(setup_client):
    client, ledger, transport = setup_client()
    with pytest.raises(LlmError, match="沒有 unknown 的單價"):
        call(client, model="unknown")
    assert not transport.calls and not ledger.path.exists()


@pytest.mark.parametrize("text", ["not json", "[]", "{}", '{"total_usd":"NaN","calls":[]}',
    '{"total_usd":"-1","calls":[]}', '{"total_usd":"0","calls":{}}',
    '{"total_usd":"1","calls":[{}]}'])
def test_corrupt_ledger_fail_closed(setup_client, text):
    client, ledger, transport = setup_client()
    ledger.path.parent.mkdir()
    ledger.path.write_text(text)
    with pytest.raises(LlmError, match="花費紀錄"):
        call(client)
    assert not transport.calls and ledger.path.read_text() == text


def test_unreadable_ledger(setup_client, monkeypatch):
    client, _, transport = setup_client()
    def denied(*args, **kwargs):
        raise PermissionError(KEY)
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(LlmError) as caught:
        call(client)
    assert KEY not in str(caught.value) and not transport.calls


@pytest.mark.parametrize("content", ['```json\n{"ok": true}\n```',
                                    'Some explanation: {"ok":true} trailing text'])
def test_json_extraction(setup_client, content):
    client, _, _ = setup_client(response(content))
    assert call(client).data == {"ok": True}


@pytest.mark.parametrize("content", ["not JSON", "", "   ", None, [], '[{"ok":true}]', "{bad}"])
def test_bad_content_still_charged(setup_client, content):
    client, ledger, _ = setup_client(response(content))
    with pytest.raises(LlmError, match="模型回覆不是有效的 JSON"):
        call(client)
    assert ledger.total() == Decimal("0.00028")
    assert len(json.loads(ledger.path.read_text())["calls"]) == 1


def test_truncation_still_charged(setup_client):
    client, ledger, _ = setup_client(response(choices=[{
        "message": {"content": '{"ok":true}'}, "finish_reason": "length"}]))
    with pytest.raises(LlmError, match="模型回覆被截斷"):
        call(client)
    assert ledger.total() == Decimal("0.00028")


@pytest.mark.parametrize("body", [b"not JSON", b"[]", b"{}", b'{"usage": null}'])
def test_malformed_200_still_charged(setup_client, body):
    client, ledger, _ = setup_client(HttpResponse(200, body))
    with pytest.raises(LlmError):
        call(client)
    assert ledger.total() > 0


@pytest.mark.parametrize("error", [{"message": "denied " + KEY}, "denied " + KEY,
                                  {"message": KEY + "x" * 300}])
def test_http_failure_redacted(setup_client, error):
    client, ledger, _ = setup_client(HttpResponse(401, json.dumps({"error": error}).encode()))
    with pytest.raises(LlmError, match="HTTP 401") as caught:
        call(client)
    assert KEY not in str(caught.value) and "***" in str(caught.value)
    assert "x" * 201 not in str(caught.value)
    assert not ledger.path.exists() and caught.value.__suppress_context__


def test_http_error_does_not_echo_prompt_or_raw_body(setup_client):
    body = json.dumps({"error": {"message": SYSTEM + " " + USER}, "private": "raw secret"}).encode()
    client, _, _ = setup_client(HttpResponse(500, body))
    with pytest.raises(LlmError) as caught:
        call(client)
    assert all(value not in str(caught.value) for value in (SYSTEM, USER, "raw secret"))


def test_http_error_does_not_echo_request_or_header_values(tmp_path, fake_transport):
    class EchoTransport(fake_transport):
        def request(self, method, url, headers, body, timeout):
            message = body.decode() + " " + " ".join(headers.values()) + " quote-sign/0.1"
            return HttpResponse(400, json.dumps({"error": {"message": message}}).encode())
    client = MixRouteClient("https://mix.example", KEY,
                            SpendLedger(tmp_path / "spend.json", Decimal(5)), EchoTransport())
    with pytest.raises(LlmError) as caught:
        call(client)
    assert all(value not in str(caught.value) for value in
               (KEY, SYSTEM, USER, "application/json", "quote-sign/0.1", '"max_tokens"'))


def test_http_non_json_error(setup_client):
    client, _, _ = setup_client(HttpResponse(502, KEY.encode()))
    with pytest.raises(LlmError, match="HTTP 502") as caught:
        call(client)
    assert KEY not in str(caught.value)


def test_network_error_redacted(setup_client):
    client, ledger, _ = setup_client(TransportError(KEY))
    with pytest.raises(LlmError) as caught:
        call(client)
    assert KEY not in str(caught.value) and not ledger.path.exists()


def test_insecure_base_url_refused(tmp_path):
    with pytest.raises(LlmError):
        MixRouteClient("http://mix.example/v1", KEY, SpendLedger(tmp_path / "spend.json", Decimal(5)))


def test_atomic_failure_preserves_previous_ledger(setup_client, monkeypatch):
    client, ledger, _ = setup_client(response(), response())
    call(client)
    original = ledger.path.read_bytes()
    def fail_replace(*args):
        raise OSError(KEY)
    monkeypatch.setattr("quote_sign.llm.os.replace", fail_replace)
    with pytest.raises(LlmError) as caught:
        call(client)
    assert KEY not in str(caught.value)
    assert ledger.path.read_bytes() == original
    assert list(ledger.path.parent.iterdir()) == [ledger.path]


def test_prices_exact():
    assert PRICES == {name: (Decimal(i), Decimal(o)) for name, i, o in [
        (MODEL, "0.14", "0.28"), ("deepseek-v4-pro", "1.74", "3.48"),
        ("gpt-5-nano", "0.05", "0.40"), ("gpt-5-mini", "0.25", "2.00"),
        ("gemini-3.5-flash-lite", "0.30", "1.20"), ("gpt-6-sol", "2.00", "10.00")]}
