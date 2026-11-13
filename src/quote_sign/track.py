"""Read signing status and retrieve completed PDFs; never sign for recipients."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from . import secrets
from .errors import SigningError
from .sign import QUOTE_NO, TAIPEI
from .storage import PrivateStore, atomic_write

FINAL = frozenset(("completed", "declined", "expired", "voided", "deleted", "unknown"))


def stage_status(task, sequence):
    return next((s.status for s in task.stages if s.sequence == sequence), "unknown")


def status(client, task_id, watch=1, sleep=None):
    if type(watch) is not int or not 1 <= watch <= 20:
        raise SigningError("watch 必須介於 1..20") from None
    sleep = time.sleep if sleep is None else sleep
    for i in range(watch):
        task = client.get_task(task_id)
        secrets.emit(f"任務 {task_id}：狀態={task.status}；買方={stage_status(task, 1)}；賣方={stage_status(task, 2)}")
        if task.status in FINAL:
            break
        if i + 1 < watch:
            sleep(30)
    return 0 if task.status in ("draft", "waiting", "completed") else 42


def fetch(client, task_id, task_log, out_dir=Path("out")):
    task = client.get_task(task_id)
    if task.status != "completed":
        secrets.emit(f"尚未完成（狀態={task.status}），收件人簽完後再取回")
        return 43
    record = task_log.find_task(task_id) or {}
    quote_no = record.get("quote_no") or f"task-{task_id}"
    if not isinstance(quote_no, str) or not QUOTE_NO.fullmatch(quote_no):
        raise SigningError("送簽紀錄報價單號無效") from None
    pdf = client.download_signed(task_id)
    if not pdf.startswith(b"%PDF-"):
        raise SigningError("下載內容不是 PDF") from None
    signed_dir = Path(out_dir) / "signed"
    store = PrivateStore(signed_dir / "signed-log.jsonl", "signed-log.lock", SigningError)
    try:
        completed_at = (datetime.fromtimestamp(task.last_modified_at, TAIPEI).isoformat()
                        if task.last_modified_at is not None else None)
    except (ValueError, OverflowError, OSError):
        raise SigningError("DottedSign 完成時間無效") from None
    line = {"recorded_at": datetime.now(timezone.utc).isoformat(), "task_id": task_id,
            "quote_no": quote_no,
            "completed_at": completed_at,
            "signers": [{"role": role, "name": record.get(name), "status": stage_status(task, seq)}
                        for role, name, seq in (("買方", "buyer_name", 1), ("賣方", "seller_name", 2))],
            "source_sha256": record.get("source_sha256"), "pdf_sha256": record.get("pdf_sha256"),
            "signed_sha256": hashlib.sha256(pdf).hexdigest()}
    encoded = secrets.mask(json.dumps(line, ensure_ascii=False))
    try:
        with store.lock():
            atomic_write(signed_dir / (quote_no + "-signed.pdf"), pdf)
            fd = os.open(store.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(encoded + "\n")
                target.flush()
                os.fsync(target.fileno())
    except OSError:
        raise SigningError("無法寫入已簽 PDF 或紀錄，拒絕符號連結") from None
    secrets.emit(encoded)
    return 0
