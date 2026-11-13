"""Bounded HTTPS requests without redirects or ambient proxy settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import QuoteSignError

MAX_RESPONSE_BYTES = 25 * 1024 * 1024


@dataclass
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str],
                body: Optional[bytes], timeout: float) -> HttpResponse:
        ...


class TransportError(QuoteSignError):
    pass


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    def __init__(self):
        self._opener = build_opener(ProxyHandler({}), NoRedirectHandler())

    def request(self, method: str, url: str, headers: dict[str, str],
                body: Optional[bytes], timeout: float) -> HttpResponse:
        if not url.startswith("https://"):
            raise TransportError("only https is allowed")
        request_headers = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
        request_headers["User-Agent"] = "quote-sign/0.1"
        try:
            request = Request(url, data=body, headers=request_headers, method=method)
            try:
                response = self._opener.open(request, timeout=timeout)
            except HTTPError as exc:
                response = exc
            with response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise TransportError("response exceeds 25 MB")
                return HttpResponse(response.code, data)
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"network error: {type(exc).__name__}") from None
        except ValueError:
            raise TransportError("invalid HTTPS request") from None
