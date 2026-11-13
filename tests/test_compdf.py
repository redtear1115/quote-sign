from __future__ import annotations

from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

from quote_sign import compdf
from quote_sign.checks import check_arithmetic, quote_from_compdf
from quote_sign.compdf import ComPdfClient
from quote_sign.errors import PdfReadError
from quote_sign.transport import HttpResponse, TransportError

KEY = "fake-compdf-credential"
DOWNLOAD_URL = "https://storage.example/result.zip?signature=fake"


def json_response(data, status=200):
    return HttpResponse(status, json.dumps(data).encode())


def upload_response(**overrides):
    data = {"code": "200", "data": {"status": "success", "downloadUrl": DOWNLOAD_URL}}
    data.update(overrides)
    return json_response(data)


def archive(data, name="nested/result_json.json"):
    target = BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("ignore.json", b"not the result")
        output.writestr(name, data)
    return target.getvalue()


@pytest.fixture
def pdf_path(tmp_path):
    path = tmp_path / "quote.pdf"
    path.write_bytes(b"%PDF-fake document bytes")
    return path


@pytest.fixture
def result_bytes():
    return (Path(__file__).parent / "fixtures" / "compdf_result_sample.json").read_bytes()


@pytest.mark.parametrize("zipped", [True, False])
def test_real_fixture_and_upload(zipped, pdf_path, result_bytes, fake_transport):
    result_body = archive(result_bytes) if zipped else result_bytes
    transport = fake_transport(upload_response(code=200), HttpResponse(200, result_body))
    parsed = ComPdfClient(KEY, transport).parse(pdf_path)
    expected_table = [
        ["項目", "數量", "單價 (NT$)", "小計 (NT$)"],
        ["MarsDawn 團隊授權 (年)", "12", "1,800", "21,600"],
        ["自訂主題設計", "1", "15,000", "15,000"],
        ["導入教學 (2 小時)", "1", "6,000", "6,000"],
    ]
    assert parsed.tables == [expected_table]
    fixture = json.loads(result_bytes)["result"]
    assert parsed.markdown == fixture["markdown"] and parsed.page_count == 1
    assert "未稅合計：NT$42,600" in parsed.paragraphs
    quote, issues = quote_from_compdf(parsed)
    assert not issues and not check_arithmetic(quote)
    assert quote.subtotal_total == Decimal(42600) and len(quote.items) == 3
    method, url, headers, body, timeout = transport.calls[0]
    assert method == "POST" and url.endswith("/v2/process/idp/documentParsing")
    header_names = set(headers)
    assert "x-api-key" in header_names
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert pdf_path.read_bytes() in body and KEY.encode() not in body
    assert b'filename="quote.pdf"' in body and b"application/pdf" in body
    assert b'name="password"\r\n\r\n\r\n--' in body
    assert b'name="language"\r\n\r\n2\r\n--' in body
    assert timeout == 120
    download = transport.calls[1]
    assert download[:2] == ("GET", DOWNLOAD_URL)
    assert not any(name.lower() == "x-api-key" for name in download[2])
    assert download[3] is None


@pytest.mark.parametrize("response, expected", [
    (upload_response(code="06001", msg="denied"), "額度用完"),
    (json_response({"code": 403, "message": "denied"}, 403), "HTTP 403"),
    (HttpResponse(200, b"private non-JSON body"), "ComPDF"),
    (upload_response(data={"status": "failed", "failureCode": "bad", "failureReason": "failed"}), "bad failed"),
    (upload_response(data={"status": "success", "downloadUrl": ""}), "回覆失敗"),
])
def test_upload_failures(response, expected, pdf_path, fake_transport):
    transport = fake_transport(response)
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(KEY, transport).parse(pdf_path)
    assert expected in str(caught.value)
    assert "private non-JSON body" not in str(caught.value)
    assert len(transport.calls) == 1


def test_echoed_key_is_redacted(pdf_path, fake_transport):
    transport = fake_transport(upload_response(code="06001", msg=KEY))
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(KEY, transport).parse(pdf_path)
    assert KEY not in str(caught.value) and "***" in str(caught.value)
    assert caught.value.__suppress_context__


def test_echoed_request_and_header_values_are_hidden(pdf_path, fake_transport):
    class EchoTransport(fake_transport):
        def request(self, method, url, headers, body, timeout):
            message = body.decode() + " " + headers["Content-Type"] + " quote-sign/0.1"
            return json_response({"code": 400, "msg": message})
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(KEY, EchoTransport()).parse(pdf_path)
    diagnostic = str(caught.value)
    assert "multipart/form-data" not in diagnostic
    assert "quote-sign/0.1" not in diagnostic and "%PDF-fake" not in diagnostic


@pytest.mark.parametrize("body", [b"PK\x03\x04invalid zip", b"{}", b"[]",
                                      archive(b"{}", "result_json.json.backup"),
                                      archive(b"not json"), b'{"result":null}'])
def test_bad_download(pdf_path, body, fake_transport):
    transport = fake_transport(upload_response(), HttpResponse(200, body))
    with pytest.raises(PdfReadError):
        ComPdfClient(KEY, transport).parse(pdf_path)


def test_zip_size_guard_before_read(pdf_path, result_bytes, fake_transport, monkeypatch):
    transport = fake_transport(upload_response(), HttpResponse(200, archive(result_bytes)))
    monkeypatch.setattr(compdf, "MAX_RESULT_BYTES", len(result_bytes) - 1)
    def unexpected(*args, **kwargs):
        pytest.fail("Oversize member must be rejected before reading")
    monkeypatch.setattr(zipfile.ZipFile, "read", unexpected)
    with pytest.raises(PdfReadError, match="大小限制"):
        ComPdfClient(KEY, transport).parse(pdf_path)


def test_pdf_size_guard(pdf_path, fake_transport, monkeypatch):
    monkeypatch.setattr(compdf, "MAX_PDF_BYTES", 4)
    transport = fake_transport()
    with pytest.raises(PdfReadError, match="20 MB"):
        ComPdfClient(KEY, transport).parse(pdf_path)
    assert not transport.calls


@pytest.mark.parametrize("error", [TransportError(KEY), OSError(KEY)])
def test_transport_error_safe(pdf_path, fake_transport, error):
    with pytest.raises(PdfReadError) as caught:
        ComPdfClient(KEY, fake_transport(error)).parse(pdf_path)
    assert KEY not in str(caught.value)


def test_download_failure(pdf_path, fake_transport):
    transport = fake_transport(upload_response(), HttpResponse(403, KEY.encode()))
    with pytest.raises(PdfReadError, match="下載失敗：HTTP 403") as caught:
        ComPdfClient(KEY, transport).parse(pdf_path)
    assert KEY not in str(caught.value)


def test_tolerant_table_and_page_fallback(pdf_path, fake_transport):
    result = {"result": {"total_page_number": 3, "detail": [
        {"type": "heading", "text": "  heading  "},
        {"type": "table", "text": "<table><tr><th>A &amp; B<th>C<tr><td> x\n y "
         "<table><tr><td>ignore</td></tr></table> z<td>one<br>two"},
    ]}}
    transport = fake_transport(upload_response(), json_response(result))
    parsed = ComPdfClient(KEY, transport).parse(pdf_path)
    assert parsed.tables == [[["A & B", "C"], ["x y z", "one two"]]]
    assert parsed.paragraphs == ["heading"] and parsed.page_count == 3


def test_zip_path_is_never_extracted(pdf_path, result_bytes, fake_transport, tmp_path):
    transport = fake_transport(upload_response(),
                               HttpResponse(200, archive(result_bytes, "../../result_json.json")))
    assert ComPdfClient(KEY, transport).parse(pdf_path).page_count == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["quote.pdf"]


def test_real_sample_signature_anchors(pdf_path, result_bytes, fake_transport):
    transport = fake_transport(upload_response(), HttpResponse(200, result_bytes))
    parsed = ComPdfClient(KEY, transport).parse(pdf_path)
    assert parsed.anchors == {"buyer": (1, (87, 1165, 593, 1196)),
                              "seller": (1, (86, 1226, 594, 1258))}
