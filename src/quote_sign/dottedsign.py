"""Sandbox only; this tool never signs on anyone's behalf.

Only POST /v1/oauth/token, POST /v1/sign_tasks, GET /v1/sign_tasks,
GET /v1/sign_tasks/download_link and the returned presigned HTTPS URL are used.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Optional
from urllib.parse import urlencode, urlsplit

from . import secrets
from .errors import SigningError, SigningQuotaExceeded
from .transport import TransportError, UrllibTransport, http_error_details

BASE_URL = "https://api.sandbox.dottedsign.com"
TASK_STATES = frozenset(("draft", "waiting", "completed", "declined", "expired", "voided", "deleted"))
STAGE_STATES = frozenset(("initial", "processing", "signed", "done", "modifying", "reviewed", "declined", "canceled"))


@dataclass(frozen=True)
class StageStatus:
    sequence: int
    status: str


@dataclass(frozen=True)
class TaskStatus:
    task_id: int
    status: str
    last_modified_at: Optional[int]
    stages: tuple


class DottedSignClient:
    def __init__(self, client_id, client_secret, transport=None, timeout=60,
                 clock=time.monotonic, sleep=time.sleep):
        self._client_id, self._client_secret = client_id, client_secret
        self._transport = transport if transport is not None else UrllibTransport()
        self._timeout, self._clock, self._sleep = timeout, clock, sleep
        self._token, self._expires = "", 0
        self._redactions = [client_id, client_secret]

    def __repr__(self):
        return "DottedSignClient(sandbox)"

    def _redact(self, message):
        for value in sorted(self._redactions + [self._token], key=len, reverse=True):
            if value:
                message = message.replace(value, "***")
        return secrets.mask(message)

    @staticmethod
    def _json(response):
        try:
            value = json.loads(response.body)
            return value if isinstance(value, dict) else {}
        except (ValueError, UnicodeError, TypeError):
            return {}

    def _error(self, operation, response):
        status, reason = http_error_details(response.status)
        message = f"DottedSign {operation}失敗：HTTP {status}（{reason}）"
        # 403 from the API means usage limit; 403 from the presigned storage URL
        # usually means the link expired, which is not a quota problem.
        error = SigningQuotaExceeded if response.status == 403 and operation != "下載" else SigningError
        exc = error(self._redact(message))
        exc.http_status = response.status
        return exc

    def _request(self, method, path, body=None, download=False):
        # 401 refresh is independent of the three-attempt GET backoff budget.
        refreshed, attempt = False, 0
        while True:
            if not download and path != "/v1/oauth/token":
                self._ensure_token()
            headers = {} if download else {"Content-Type": "application/json"}
            if not download and path != "/v1/oauth/token":
                headers["Authorization"] = "Bearer " + self._token
            failed_transport = False
            try:
                response = self._transport.request(method, path if download else BASE_URL + path,
                                                   headers, body, self._timeout)
            except TransportError:
                failed_transport = True
            except Exception:
                failed_transport = "unexpected"
            if failed_transport:
                if failed_transport is True and method == "GET" and attempt < 2:
                    attempt += 1
                    self._sleep(attempt)
                    continue
                message = "DottedSign 網路失敗"
                if method == "POST" and path == "/v1/sign_tasks":
                    message += "：任務可能已建立（MAY have been created）；請先用 quote-sign status / DottedSign web UI 檢查，再決定是否重送"
                exc = SigningError(self._redact(message))
                exc.unknown_outcome = True
                raise exc from None
            if response.status == 401 and not download and path != "/v1/oauth/token" and not refreshed:
                self._token = ""
                refreshed = True
                continue
            if (method == "GET" and type(response.status) is int and
                    (response.status == 429 or 500 <= response.status <= 599) and attempt < 2):
                attempt += 1
                self._sleep(attempt)
                continue
            if response.status != 200:
                raise self._error("下載" if download else "請求", response) from None
            return response

    def _ensure_token(self):
        if self._token and self._clock() < self._expires - 60:
            return
        body = json.dumps({"grant_type": "client_credentials", "client_id": self._client_id,
                           "client_secret": self._client_secret}).encode()
        data = self._json(self._request("POST", "/v1/oauth/token", body))
        token, expires = data.get("access_token"), data.get("expires_in")
        if (not isinstance(token, str) or not token or type(expires) not in (int, float)
                or not math.isfinite(expires) or expires <= 0 or data.get("token_type") != "Bearer"):
            raise SigningError(self._redact("DottedSign token 回覆格式無效")) from None
        self._token, self._expires = token, self._clock() + expires
        self._redactions.append(token)

    def _task_id(self, task_id):
        if type(task_id) is not int or task_id <= 0:
            raise SigningError(self._redact("DottedSign task_id 必須是正整數")) from None
        return task_id

    def create_task(self, body: dict) -> int:
        data = self._json(self._request("POST", "/v1/sign_tasks", json.dumps(body).encode())).get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        return self._task_id(task_id)

    def get_task(self, task_id: int) -> TaskStatus:
        task_id = self._task_id(task_id)
        data = self._json(self._request("GET", "/v1/sign_tasks?" + urlencode({"task_id": task_id}))).get("data")
        if not isinstance(data, dict) or type(data.get("task_id")) is not int or data["task_id"] != task_id:
            raise SigningError(self._redact("DottedSign 任務回覆格式無效")) from None
        stages = []
        details = data.get("details", [])
        if not isinstance(details, list):
            raise SigningError(self._redact("DottedSign 任務回覆格式無效")) from None
        for detail in details:
            if isinstance(detail, dict) and type(detail.get("sequence")) is int and detail["sequence"] >= 1:
                status = detail.get("status")
                stages.append(StageStatus(detail["sequence"], status if isinstance(status, str) and status in STAGE_STATES else "unknown"))
        status, modified = data.get("status"), data.get("last_modified_at")
        return TaskStatus(task_id, status if isinstance(status, str) and status in TASK_STATES else "unknown",
                          modified if type(modified) is int else None,
                          tuple(sorted(stages, key=lambda s: s.sequence)))

    def download_signed(self, task_id: int) -> bytes:
        query = urlencode({"task_id": self._task_id(task_id), "file_types[]": "task"})
        data = self._json(self._request("GET", "/v1/sign_tasks/download_link?" + query)).get("data")
        link = data.get("task") if isinstance(data, dict) else None
        valid = False
        if isinstance(link, str):
            try:
                parts = urlsplit(link)
                valid = parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
            except ValueError:
                pass
        if not valid:
            raise SigningError(self._redact("DottedSign 下載連結無效，必須為 HTTPS")) from None
        response = self._request("GET", link, download=True)
        if not response.body.startswith(b"%PDF-"):
            raise SigningError(self._redact("DottedSign 下載內容不是 PDF")) from None
        return response.body
