"""Build sandbox requests and record attempts before any task creation."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Optional, Protocol
from uuid import uuid4

from . import secrets
from .errors import SigningError, SigningNotConfigured, SigningQuotaExceeded
from .storage import PrivateStore

MAX_PDF_BYTES = 20 * 1024 * 1024
QUOTE_NO = re.compile(r"[A-Za-z0-9-]{1,40}")
TAIPEI = timezone(timedelta(hours=8))


@dataclass(frozen=True, repr=False)
class SignRequest:
    pdf_path: Path
    source_sha256: str
    quote_no: str
    buyer_name: str
    seller_name: str
    valid_until: Optional[date]
    buyer_anchor: Optional[tuple]
    seller_anchor: Optional[tuple]

    def __repr__(self):
        return "SignRequest(redacted)"


@dataclass(frozen=True)
class SignOutcome:
    task_id: Optional[int]
    dry_run: bool
    reused: bool


def field_coords(anchor):
    try:
        page, box = anchor
        x0, y0, x1, y1 = box
        if (type(page) is not int or page < 1
                or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
                or not (0 <= x0 < x1 <= 1190 and 0 <= y0 < y1 <= 1684)
                or 842 - y0 * .5 + 10 > 842):
            raise ValueError()
    except (ValueError, TypeError):
        raise SigningError("找不到簽名位置：缺少錨點或位置超出頁面") from None
    unit = (x1 - x0) * .5 / 21
    left, bottom, top = x0 * .5, 842 - y1 * .5, 842 - y0 * .5
    signature = [round(v, 1) for v in (left + 4 * unit, bottom, left + 13 * unit, top + 10)]
    date_coord = [round(v, 1) for v in (left + 16 * unit, bottom, left + 21 * unit, top)]
    return page - 1, signature, date_coord


def build_task_body(req, buyer_email, seller_email, pdf_bytes, run_id):
    for name in (req.buyer_name, req.seller_name):
        if not isinstance(name, str) or secrets._EMAIL.search(name) or "@" in name:
            raise SigningError("簽署人顯示名稱含信箱，拒絕送簽") from None
    if not isinstance(req.quote_no, str) or not QUOTE_NO.fullmatch(req.quote_no):
        raise SigningError("報價單號無效，無法建立送簽檔名") from None
    stages = []
    for name, email, anchor in ((req.buyer_name, buyer_email, req.buyer_anchor),
                                 (req.seller_name, seller_email, req.seller_anchor)):
        page, signature, date_coord = field_coords(anchor)
        stages.append({"name": name, "email": email, "stage_type": "sign", "field_settings": [
            {"field_type": "signature", "page": page, "coord": signature, "options": {"force": True}},
            {"field_type": "datefield", "page": page, "coord": date_coord,
             "options": {"force": True, "date_format": "yyyy-mm-dd"}}]})
    setting = {"receiver_lang": "zh-TW", "message": "這是測試環境（sandbox）的範例報價單，內容為假資料。"}
    if req.valid_until is not None:
        setting["deadline"] = int(datetime.combine(req.valid_until, time(23, 59, 59), TAIPEI).timestamp())
    return {"file_name": f"quote-{req.quote_no}.pdf", "base64_file": base64.b64encode(pdf_bytes).decode("ascii"),
            "has_order": True, "start": True, "stages": stages,
            "metadata": {"quote_no": req.quote_no, "source_sha256": req.source_sha256[:40], "quote_sign_run": run_id},
            "task_setting": setting}


def describe_task_body(body):
    safe = {**body, "base64_file": f"<PDF base64，{len(base64.b64decode(body['base64_file']))} bytes，省略>",
            "stages": [{**stage, "email": secrets.mask_email(stage["email"])} for stage in body["stages"]]}
    return [secrets.mask(line) for line in
            ["POST https://api.sandbox.dottedsign.com/v1/sign_tasks"]
            + json.dumps(safe, ensure_ascii=False, indent=2).splitlines()]


class TaskLog(PrivateStore):
    def __init__(self, path):
        super().__init__(path, "sign-tasks.lock", SigningError)

    def _read(self):
        try:
            self.guard()
            if not self.path.exists():
                return []
            records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
            for r in records:
                ts = datetime.fromisoformat(r["ts"])
                if (r["state"] not in ("sending", "created", "failed", "unknown")
                        or ts.utcoffset() is None
                        or not all(isinstance(r[k], str) for k in
                                   ("run_id", "quote_no", "source_sha256", "pdf_sha256", "buyer_name", "seller_name"))
                        or (r["task_id"] is not None and (type(r["task_id"]) is not int or r["task_id"] <= 0))):
                    raise ValueError()
            return records
        except (OSError, ValueError, TypeError, KeyError):
            raise SigningError("送簽紀錄損毀或無法讀取，拒絕送簽") from None

    def _append(self, record):
        self.guard()
        safe = {key: secrets.mask(value) if isinstance(value, str) else value
                for key, value in record.items()}
        line = json.dumps(safe, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(line)
            target.flush()
            os.fsync(target.fileno())

    def count_last_24h(self, now):
        with self.lock():
            first = {}
            for r in self._read():
                first.setdefault(r["run_id"], datetime.fromisoformat(r["ts"]))
            return sum(now - timedelta(hours=24) <= ts <= now for ts in first.values())

    def find_active(self, quote_no, source_sha256):
        with self.lock():
            last = {}
            for r in self._read():
                last[r["run_id"]] = r
            return next((r for r in reversed(list(last.values())) if r["quote_no"] == quote_no
                         and r["source_sha256"] == source_sha256 and r["state"] in ("created", "unknown")), None)

    def find_task(self, task_id):
        with self.lock():
            return next((r for r in reversed(self._read()) if r["state"] == "created" and r["task_id"] == task_id), None)

    def begin(self, *, run_id, quote_no, source_sha256, pdf_sha256, buyer_name, seller_name, now=None):
        with self.lock():
            self._read()
            self._append({"ts": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
                          "run_id": run_id, "state": "sending", "task_id": None, "quote_no": quote_no,
                          "source_sha256": source_sha256, "pdf_sha256": pdf_sha256,
                          "buyer_name": buyer_name, "seller_name": seller_name})

    def finish(self, run_id, state, task_id=None):
        with self.lock():
            if state not in ("created", "failed", "unknown"):
                raise SigningError("送簽紀錄狀態無效") from None
            record = next((r for r in reversed(self._read()) if r["run_id"] == run_id), None)
            if record is None:
                raise SigningError("找不到送簽紀錄") from None
            self._append({**record, "ts": datetime.now(timezone.utc).isoformat(), "state": state, "task_id": task_id})


class Signer(Protocol):
    def send(self, req: SignRequest, *, dry_run: bool, resend: bool = False) -> SignOutcome:
        ...


class RefusingSigner:
    def send(self, req, *, dry_run, resend=False):
        raise SigningNotConfigured("送簽尚未設定：需要 QUOTE_SIGN_BUYER_EMAIL / QUOTE_SIGN_SELLER_EMAIL 與 DottedSign sandbox 設定") from None


class DottedSignSigner:
    def __init__(self, client_factory, buyer_email, seller_email, task_log, local_limit=15, now=None):
        self._client_factory = client_factory
        self._buyer_email, self._seller_email = buyer_email, seller_email
        self.task_log, self.local_limit = task_log, local_limit
        self._now = now or (lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return "DottedSignSigner(sandbox)"

    def send(self, req, *, dry_run, resend=False):
        try:
            with req.pdf_path.open("rb") as source:
                pdf = source.read(MAX_PDF_BYTES + 1)
        except OSError:
            raise SigningError("無法讀取送簽 PDF") from None
        if len(pdf) > MAX_PDF_BYTES or not pdf.startswith(b"%PDF-"):
            raise SigningError("送簽 PDF 無效或超過 20 MB") from None
        run_id = uuid4().hex[:12]
        body = build_task_body(req, self._buyer_email, self._seller_email, pdf, run_id)
        if dry_run:
            for line in describe_task_body(body):
                secrets.emit(line)
            secrets.emit("送簽 dry-run：沒有送出任何 DottedSign 請求，也沒有建立任務。")
            return SignOutcome(None, True, False)
        with self.task_log.lock():
            active = self.task_log.find_active(req.quote_no, req.source_sha256)
            if active and not resend:
                if active["state"] == "unknown":
                    raise SigningError("上一次送簽結果不明（沒有 task_id）；請先到 DottedSign web UI 或用 quote-sign status 檢查，確認沒有建立任務後再加 --resend") from None
                secrets.emit(f"已有送簽任務 task_id={active['task_id']}（狀態={active['state']}）；需加 --resend 才會另建任務。請先查 DottedSign web UI。")
                return SignOutcome(active["task_id"], False, True)
            now = self._now() if callable(self._now) else self._now
            if self.task_log.count_last_24h(now) >= self.local_limit:
                raise SigningQuotaExceeded("DottedSign 本機過去 24 小時送簽已達上限，未送出") from None
            self.task_log.begin(run_id=run_id, quote_no=req.quote_no, source_sha256=req.source_sha256,
                                pdf_sha256=hashlib.sha256(pdf).hexdigest(), buyer_name=req.buyer_name,
                                seller_name=req.seller_name, now=now)
        failure = None
        try:
            task_id = self._client_factory().create_task(body)
            if type(task_id) is not int or task_id <= 0:
                raise SigningError("DottedSign 建任務回覆的 task_id 無效") from None
        except BaseException as exc:
            failed = isinstance(exc, SigningQuotaExceeded) or (isinstance(exc, SigningError)
                     and 400 <= getattr(exc, "http_status", 0) < 500)
            self.task_log.finish(run_id, "failed" if failed else "unknown")
            failure = exc
        if failure is not None:
            if isinstance(failure, SigningError):
                message = str(failure)
                for email in (self._buyer_email, self._seller_email):
                    message = re.sub(re.escape(email), lambda _: secrets.mask_email(email), message, flags=re.I)
                failure.args = (secrets.mask(message),)
            elif isinstance(failure, KeyboardInterrupt):
                failure.args = ()
            else:
                # Keep the original exception type while discarding arbitrary
                # transport/factory text, which can contain credentials or URLs.
                failure.args = ("DottedSign 送簽結果不明；請先用 quote-sign status / DottedSign web UI 檢查",)
            failure.__context__ = None
            failure.__cause__ = None
            raise failure from None
        self.task_log.finish(run_id, "created", task_id)
        secrets.emit(f"第 4 步（送簽）已建立任務 task_id={task_id}：買方 {secrets.mask_email(self._buyer_email)} → 賣方 {secrets.mask_email(self._seller_email)}，等待簽署（簽名由收件人自己完成，本工具不代簽）")
        return SignOutcome(task_id, False, False)
