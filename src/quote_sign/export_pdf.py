from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from .errors import ExportError, InputRejected
from .input_file import InputDoc
from .safe_paths import atomic_write_at, open_dir, write_all


@dataclass(frozen=True)
class ExportResult:
    path: Path
    pages: int
    diagram_errors: list[str]


_MAX_PDF_BYTES = 100 * 1024 * 1024
_ERROR_KINDS = frozenset(("input_not_found", "output_exists", "export_failed", "usage",
                          "timeout", "invalid_input"))


def _read_pdf(path):
    try:
        # Nonblocking prevents a malformed FIFO output from hanging before fstat.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ELOOP):
            raise ExportError("marsdawn 的輸出無效") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PDF_BYTES:
            raise ExportError("marsdawn 的輸出無效") from None
        chunks = []
        remaining = _MAX_PDF_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        pdf = b"".join(chunks)
        if len(pdf) > _MAX_PDF_BYTES or not pdf.startswith(b"%PDF-"):
            raise ExportError("marsdawn 的輸出無效") from None
        return pdf
    finally:
        os.close(fd)


def export_pdf(doc: InputDoc, out_dir: Path, runner=subprocess.run) -> ExportResult:
    try:
        out_fd = open_dir(out_dir, create=True)
        try:
            final = Path(doc.name).stem + ".pdf"
            with tempfile.TemporaryDirectory(prefix="quote-sign-src-") as private_dir:
                private_copy = Path(private_dir) / doc.name
                private_output = Path(private_dir) / final
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                fd = os.open(private_copy, flags, 0o600)
                try:
                    write_all(fd, doc.data)
                finally:
                    os.close(fd)
                # Only the checked Markdown snapshot is available for relative images.
                try:
                    result = runner(
                        ["marsdawn", "export", str(private_copy), "-o", str(private_output), "--force", "--json"],
                        capture_output=True, text=True, timeout=120,
                    )
                except FileNotFoundError:
                    raise ExportError("找不到 marsdawn，請執行 brew install redtear1115/tap/marsdawn") from None
                try:
                    lines = [line for line in result.stdout.splitlines() if line.strip()]
                    payload = json.loads(lines[-1])
                    if not isinstance(payload, dict):
                        raise ValueError("JSON object required")
                except (ValueError, IndexError, AttributeError):
                    raise ExportError("marsdawn 未回傳有效的 JSON 結果") from None
                if result.returncode != 0 or payload.get("ok") is not True:
                    kind = payload.get("error") or {
                        2: "input_not_found", 4: "output_exists", 5: "export_failed", 64: "usage",
                    }.get(result.returncode, "export_failed")
                    if not isinstance(kind, str) or kind not in _ERROR_KINDS:
                        kind = "export_failed"
                    raise ExportError(f"{kind}：轉檔失敗") from None
                try:
                    pages = int(payload.get("pages", 0))
                    errors = payload.get("diagramErrors", [])
                    if not isinstance(errors, list):
                        raise ValueError("diagramErrors must be a list")
                except (ValueError, TypeError, OverflowError):
                    raise ExportError("marsdawn 的輸出路徑或頁數無效") from None
                pdf = _read_pdf(private_output)
                atomic_write_at(out_fd, final, pdf)
            return ExportResult(Path(out_dir) / final, pages, [str(error) for error in errors])
        finally:
            os.close(out_fd)
    except InputRejected as exc:
        raise ExportError(f"輸出目錄不符規則（{exc.category}）") from None
    except subprocess.TimeoutExpired:
        raise ExportError("轉檔逾時") from None
    except subprocess.SubprocessError:
        raise ExportError("轉檔無法完成") from None
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            message = "轉檔無法完成：磁碟空間不足"
        elif exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            message = "轉檔無法完成：權限不足"
        else:
            message = "轉檔無法完成：檔案系統錯誤"
        raise ExportError(message) from None
