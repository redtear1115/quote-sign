from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .errors import ExportError
from .input_file import InputDoc


@dataclass(frozen=True)
class ExportResult:
    path: Path
    pages: int
    diagram_errors: list[str]


def export_pdf(doc: InputDoc, out_dir: Path, runner=subprocess.run) -> ExportResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (Path(doc.name).stem + ".pdf")
    try:
        with tempfile.TemporaryDirectory(prefix="quote-sign-src-") as private_dir:
            private_copy = Path(private_dir) / doc.name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(private_copy, flags, 0o600)
            try:
                remaining = memoryview(doc.data)
                while remaining:
                    written = os.write(fd, remaining)
                    remaining = remaining[written:]
            finally:
                os.close(fd)
            # marsdawn reads the checked bytes; relative images resolve in a folder containing only this Markdown file.
            result = runner(
                ["marsdawn", "export", str(private_copy), "-o", str(pdf_path), "--force", "--json"],
                capture_output=True, text=True, timeout=120,
            )
    except FileNotFoundError as exc:
        raise ExportError("找不到 marsdawn，請執行 brew install redtear1115/tap/marsdawn") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise ExportError("轉檔無法完成：" + str(exc)) from exc
    try:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        if not isinstance(payload, dict):
            raise ValueError("JSON object required")
    except (ValueError, IndexError, AttributeError) as exc:
        raise ExportError("marsdawn 未回傳有效的 JSON 結果") from exc
    if result.returncode != 0 or payload.get("ok") is not True:
        kind = payload.get("error") or {
            2: "input_not_found", 4: "output_exists", 5: "export_failed", 64: "usage",
        }.get(result.returncode, "export_failed")
        raise ExportError(f"{kind}：{payload.get('message') or '轉檔失敗'}")
    try:
        path = Path(payload["output"])
        pages = int(payload.get("pages", 0))
        errors = payload.get("diagramErrors", [])
        if not isinstance(errors, list):
            raise ValueError("diagramErrors must be a list")
    except (KeyError, ValueError, TypeError) as exc:
        raise ExportError("marsdawn 的輸出路徑或頁數無效") from exc
    return ExportResult(path, pages, [str(error) for error in errors])
