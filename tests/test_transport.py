from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from quote_sign import transport
from quote_sign.transport import NoRedirectHandler, TransportError, UrllibTransport


@pytest.mark.parametrize("url", ["http://example.com", "file:///tmp/pdf", "ftp://example.com"])
def test_only_https(url):
    with pytest.raises(TransportError, match="only https is allowed"):
        UrllibTransport().request("GET", url, {}, None, 1)


def test_redirect_handler():
    handler = NoRedirectHandler()
    assert handler.redirect_request(Request("https://first.example"), None, 302, "Found",
                                    {"Location": "https://second.example"},
                                    "https://second.example") is None


def test_offline_http_error_is_response_and_user_agent(monkeypatch):
    client = UrllibTransport()
    observed = []
    def open_request(request, timeout):
        observed.append((request, timeout))
        raise HTTPError(request.full_url, 403, "denied", {}, BytesIO(b"response"))
    monkeypatch.setattr(client._opener, "open", open_request)
    result = client.request("POST", "https://example.com", {"User-Agent": "custom"}, b"body", 2)
    assert result.status == 403 and result.body == b"response"
    assert observed[0][0].get_header("User-agent") == "quote-sign/0.1"
    assert observed[0][1] == 2


@pytest.mark.parametrize("error", [URLError("sensitive"), TimeoutError("sensitive"), OSError("sensitive")])
def test_safe_network_errors(monkeypatch, error):
    client = UrllibTransport()
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(client._opener, "open", fail)
    with pytest.raises(TransportError) as caught:
        client.request("GET", "https://example.com", {}, None, 1)
    assert str(caught.value) == "network error: " + type(error).__name__


def test_response_size_guard(monkeypatch):
    client = UrllibTransport()
    monkeypatch.setattr(transport, "MAX_RESPONSE_BYTES", 4)
    def oversized(*args, **kwargs):
        raise HTTPError("https://example.com", 200, "ok", {}, BytesIO(b"12345"))
    monkeypatch.setattr(client._opener, "open", oversized)
    with pytest.raises(TransportError, match="25 MB"):
        client.request("GET", "https://example.com", {}, None, 1)


@pytest.mark.parametrize("name", ["compdf", "llm", "transport"])
def test_clients_have_no_ambient_inputs_or_output(name):
    path = Path(__file__).resolve().parents[1] / "src" / "quote_sign" / (name + ".py")
    source = path.read_text()
    assert all(forbidden not in source for forbidden in ("environ", "getenv", "print(", "logging"))
