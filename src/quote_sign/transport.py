"""Bounded HTTPS requests without redirects or ambient proxy settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .errors import QuoteSignError

MAX_RESPONSE_BYTES = 25 * 1024 * 1024


def http_error_details(status: object) -> tuple[str, str]:
    """Return diagnostics derived only from an integer HTTP status."""
    if type(status) is not int:
        return "?", "其他錯誤"
    reasons = {
        400: "請求無效", 401: "金鑰無效或未授權", 402: "額度不足", 403: "沒有權限",
        404: "模型或端點不存在", 408: "逾時", 413: "請求過大", 429: "請求過多或額度用完",
    }
    return str(status), "服務端錯誤" if 500 <= status <= 599 else reasons.get(status, "其他錯誤")


@dataclass(repr=False)
class HttpResponse:
    status: int
    body: bytes

    def __repr__(self):
        return "HttpResponse(redacted)"


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
