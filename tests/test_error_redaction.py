"""Remote diagnostics must not escape through exceptions, output, or logs."""
from decimal import Decimal
import json
import logging
import traceback
from types import SimpleNamespace

import pytest

from quote_sign import cli
from quote_sign.compdf import ComPdfClient
from quote_sign.dottedsign import BASE_URL, DottedSignClient
from quote_sign.errors import LlmError, PdfReadError, SigningError, SigningQuotaExceeded
from quote_sign.llm import MixRouteClient, SpendLedger
from quote_sign.transport import HttpResponse, TransportError


QUOTE_CANARY = "CANARY-QUOTE-BODY-7f3a"
CANARIES = (QUOTE_CANARY, "x@example.invalid", "sk-canary-0123456789abcdef0123",
            "canary_quote_body_7f3a", "987654")
BODY_TEXT = " ".join(CANARIES)
HTTP_REASONS = {
    400: "請求無效", 401: "金鑰無效或未授權", 402: "額度不足", 403: "沒有權限",
    404: "模型或端點不存在", 408: "逾時", 413: "請求過大", 429: "請求過多或額度用完",
    500: "服務端錯誤", 503: "服務端錯誤", 599: "服務端錯誤",
    302: "其他錯誤", 418: "其他錯誤", 600: "其他錯誤",
}
ERROR_KINDS = list(HTTP_REASONS) + [
    "network", "oserror", "timeout", "invalid_json", "empty", "invalid_json_400", "empty_500",
]


def canary_payload(**overrides):
    payload = {
        "quote": BODY_TEXT, "error": {"message": BODY_TEXT}, "message": BODY_TEXT,
        "error_message": BODY_TEXT, "error_key": "canary_quote_body_7f3a", "error_code": 987654,
        "code": QUOTE_CANARY, "msg": BODY_TEXT,
        "data": {"status": "failed", "failureCode": BODY_TEXT, "failureReason": BODY_TEXT},
    }
    payload.update(overrides)
    return payload


def reply(status=200, **overrides):
    return HttpResponse(status, json.dumps(canary_payload(**overrides)).encode())


def failure(kind):
    if type(kind) is int:
        return reply(kind)
    if kind == "network":
        return TransportError("network error: " + BODY_TEXT)
    if kind == "oserror":
        return OSError(BODY_TEXT)
    if kind == "timeout":
        return TimeoutError(BODY_TEXT)
    if kind.startswith("invalid_json"):
        return HttpResponse(400 if kind.endswith("400") else 200,
                            b"not JSON: " + json.dumps(canary_payload()).encode())
    assert kind in ("empty", "empty_500")
    return HttpResponse(500 if kind.endswith("500") else 200, b"")


def assert_clean(exc, capsys, caplog):
    captured = capsys.readouterr()
    surfaces = (str(exc), repr(exc),
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                captured.out + captured.err, caplog.text)
    for canary in CANARIES:
        for surface in surfaces:
            assert canary not in surface
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True or exc.__context__ is None
    return captured


def assert_http_reason(exc, kind):
    status = kind if type(kind) is int else {"invalid_json_400": 400, "empty_500": 500}.get(kind)
    if status is not None:
        assert f"HTTP {status}（{HTTP_REASONS[status]}）" in str(exc)


def mixroute(tmp_path, transport):
    ledger = SpendLedger(tmp_path / "state" / "spend.json", Decimal("5"))
    return MixRouteClient("https://mixroute.invalid/v1", "fake-api-key", ledger, transport), ledger


def chat(client):
    return client.chat_json(model="deepseek-v4-flash", purpose="test", system="system",
                            user="user", max_tokens=100)


@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_mixroute_errors(kind, tmp_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    client, ledger = mixroute(tmp_path, fake_transport(failure(kind)))
    with pytest.raises(LlmError) as caught:
        chat(client)
    assert_clean(caught.value, capsys, caplog)
    assert_http_reason(caught.value, kind)
    if type(kind) is int or kind in ("invalid_json_400", "empty_500"):
        stored = json.loads(ledger.path.read_text())
        assert len(stored["calls"]) == 1
        assert stored["calls"][0]["pending"] is True
        worst_case = (10 * Decimal("0.14") + 100 * Decimal("0.28")) / 1_000_000
        assert ledger.total() == worst_case


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "quote.pdf"
    path.write_bytes(b"%PDF-fake")
    return path


@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_compdf_errors(kind, pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    client = ComPdfClient(api_key="fake-public-key", transport=fake_transport(failure(kind)))
    with pytest.raises(PdfReadError) as caught:
        client.parse(pdf_path)
    assert_clean(caught.value, capsys, caplog)
    assert_http_reason(caught.value, kind)
    if kind in ("invalid_json", "empty"):
        assert str(caught.value) == "ComPDF 回覆失敗：HTTP 200（處理失敗）"


@pytest.mark.parametrize("payload", [
    canary_payload(), [canary_payload()], BODY_TEXT,
    canary_payload(code="200"), canary_payload(code="200", data=None),
    canary_payload(code="200", data=[BODY_TEXT]),
    canary_payload(code="200", data={"status": "success", "failureReason": BODY_TEXT}),
    canary_payload(code="200", data={"status": BODY_TEXT, "downloadUrl": BODY_TEXT}),
])
def test_compdf_processing_failure(payload, pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    transport = fake_transport(HttpResponse(200, json.dumps(payload).encode()))
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(api_key="fake-public-key", transport=transport).parse(pdf_path)
    assert_clean(caught.value, capsys, caplog)
    assert str(caught.value) == "ComPDF 回覆失敗：HTTP 200（處理失敗）"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [200, 400, 403, 500])
@pytest.mark.parametrize("code", ["06001", 6001])
def test_compdf_free_quota(status, code, pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(api_key="fake-public-key", transport=fake_transport(
            reply(status, code=code))).parse(pdf_path)
    assert_clean(caught.value, capsys, caplog)
    assert str(caught.value) == f"ComPDF 回覆失敗：HTTP {status}（免費額度用完）"


@pytest.mark.parametrize("code", ["6001", " 06001", "06001 ", 6001.0, ["06001"]])
def test_compdf_quota_code_is_exact(code, pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(api_key="fake-public-key", transport=fake_transport(
            reply(403, code=code))).parse(pdf_path)
    assert_clean(caught.value, capsys, caplog)
    assert str(caught.value) == "ComPDF 回覆失敗：HTTP 403（沒有權限）"


def test_compdf_download_failure(pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    url = "https://download.invalid/" + QUOTE_CANARY
    transport = fake_transport(reply(code="200", data={"status": "success", "downloadUrl": url}),
                               reply(403))
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(api_key="fake-public-key", transport=transport).parse(pdf_path)
    assert_clean(caught.value, capsys, caplog)
    assert str(caught.value) == "ComPDF 下載失敗：HTTP 403"
    assert url not in str(caught.value) and "https://" not in str(caught.value)
    assert transport.calls[-1][:3] == ("GET", url, {})


def token():
    return reply(access_token="fake-token", token_type="Bearer", expires_in=3600)


def dotted_scenario(fake_transport, operation, failed_response):
    responses = [] if operation == "token" else [token()]
    url = "https://storage.invalid/" + QUOTE_CANARY + ".pdf"
    if operation == "download":
        responses.append(reply(data={"task": url}))
    if (isinstance(failed_response, HttpResponse) and failed_response.status == 401
            and operation not in ("token", "download")):
        responses.extend([failed_response, token(), failed_response])
    else:
        responses.extend([failed_response] * 3)
    transport = fake_transport(*responses)
    sleeps = []
    client = DottedSignClient("fake-client-id", "fake-client-secret", transport, sleep=sleeps.append)
    return client, transport, sleeps


def call_dotted(client, operation):
    if operation == "create":
        return client.create_task({})
    if operation in ("download_link", "download"):
        return client.download_signed(407)
    return client.get_task(407)


@pytest.mark.parametrize("operation", ["token", "get", "create", "download_link", "download"])
@pytest.mark.parametrize("kind", ERROR_KINDS)
def test_dottedsign_errors(operation, kind, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    client, transport, sleeps = dotted_scenario(fake_transport, operation, failure(kind))
    with pytest.raises(SigningError) as caught:
        call_dotted(client, operation)
    assert_clean(caught.value, capsys, caplog)
    assert_http_reason(caught.value, kind)
    assert "https://" not in str(caught.value)
    assert transport.calls[0][:2] == ("POST", BASE_URL + "/v1/oauth/token")
    if type(kind) is int:
        assert caught.value.http_status == kind
        assert isinstance(caught.value, SigningQuotaExceeded) == (kind == 403 and operation != "download")
    if kind in (401, 429, 500, 503, 599, "network"):
        retry = operation in ("get", "download_link", "download") and kind != 401
        assert sleeps == ([1, 2] if retry else [])


@pytest.mark.parametrize("status", [QUOTE_CANARY, None, True, 400.0])
@pytest.mark.parametrize("service", ["mixroute", "compdf", "dottedsign"])
def test_non_integer_http_status(service, status, tmp_path, pdf_path, fake_transport, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    response = reply(status)
    if service == "mixroute":
        client, _ = mixroute(tmp_path, fake_transport(response))
        call = lambda: chat(client)
        error = LlmError
    elif service == "compdf":
        client = ComPdfClient(api_key="fake-public-key", transport=fake_transport(response))
        call = lambda: client.parse(pdf_path)
        error = PdfReadError
    else:
        client, _, _ = dotted_scenario(fake_transport, "get", response)
        call = lambda: client.get_task(407)
        error = SigningError
    with pytest.raises(error) as caught:
        call()
    assert_clean(caught.value, capsys, caplog)
    assert "HTTP ?（其他錯誤）" in str(caught.value)


@pytest.mark.parametrize("command", ["status", "fetch"])
@pytest.mark.parametrize("status,exit_code", [(500, 40), (403, 41)])
def test_cli_tracking_errors(command, status, exit_code, tmp_path, fake_transport,
                             monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    for name in cli.DOTTEDSIGN_NAMES:
        monkeypatch.setenv(name, "fake-dottedsign-credential")
    client, _, _ = dotted_scenario(fake_transport, "get", reply(status))
    errors = []

    def get_task(task_id):
        try:
            return client.get_task(task_id)
        except SigningError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(cli, "build_signing_client", lambda: SimpleNamespace(get_task=get_task))
    args = [command, "407"]
    if command == "fetch":
        args += ["--out-dir", str(tmp_path / "o")]
    assert cli.main(args) == exit_code
    assert len(errors) == 1
    captured = assert_clean(errors[0], capsys, caplog)
    assert f"HTTP {status}（{HTTP_REASONS[status]}）" in captured.err
    assert not captured.out


def test_cli_unexpected_error(sample_path, monkeypatch, capsys, caplog):
    caplog.set_level(logging.DEBUG)

    def build(_):
        raise RuntimeError(BODY_TEXT)

    monkeypatch.setattr(cli, "build_real_deps", build)
    assert cli.main([str(sample_path)]) == 70
    captured = capsys.readouterr()
    assert captured.err == "未預期的錯誤：RuntimeError\n"
    assert not captured.out
    for canary in CANARIES:
        assert canary not in captured.out + captured.err + caplog.text
