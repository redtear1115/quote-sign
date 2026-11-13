"""Public key only in x-api-key; download URL gets no key; ZIP handled in memory."""
from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from typing import Optional
from uuid import uuid4
import zipfile

from .errors import PdfReadError
from .models import ParsedPdf
from .transport import Transport, UrllibTransport

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_RESULT_BYTES = 25 * 1024 * 1024


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = []
        self.cell = None
        self.depth = 0

    def _cell_end(self):
        if self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None

    def _row_end(self):
        self._cell_end()
        if self.row:
            self.rows.append(self.row)
            self.row = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
        elif self.depth == 1:
            if tag == "tr":
                self._row_end()
            elif tag in ("td", "th"):
                self._cell_end()
                self.cell = []
            elif tag in ("br", "p", "div") and self.cell is not None:
                self.cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            if self.depth == 1:
                self._row_end()
            self.depth = max(0, self.depth - 1)
        elif self.depth == 1:
            if tag in ("td", "th"):
                self._cell_end()
            elif tag == "tr":
                self._row_end()

    def handle_data(self, data):
        if self.depth == 1 and self.cell is not None:
            self.cell.append(data)


class ComPdfClient:
    def __init__(self, api_key: str, transport: Optional[Transport] = None,
                 base_url: str = "https://api-server.compdf.com/server", timeout: float = 120):
        self._api_key = api_key
        self._transport = transport if transport is not None else UrllibTransport()
        self._base_url, self._timeout = base_url.rstrip("/"), timeout

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def parse(self, pdf_path: Path) -> ParsedPdf:
        try:
            return self._parse(Path(pdf_path))
        except PdfReadError as exc:
            raise PdfReadError(self._redact(str(exc))) from None
        except Exception as exc:
            # Only the exception type is forwarded: library messages may carry paths or response data.
            raise PdfReadError(self._redact(
                f"ComPDF 讀取失敗：檔案、網路或結果格式無效（{type(exc).__name__}）")) from None

    def _parse(self, path: Path) -> ParsedPdf:
        with path.open("rb") as source:
            pdf = source.read(MAX_PDF_BYTES + 1)
        if len(pdf) > MAX_PDF_BYTES:
            raise PdfReadError(self._redact("PDF 超過 20 MB"))
        boundary = uuid4().hex
        filename = path.name.replace('"', "_").replace("\\", "_").replace("\r", "_").replace("\n", "_")
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n').encode() + pdf
        for name, value in (("password", ""), ("language", "2")):
            body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                     f'\r\n\r\n{value}').encode()
        body += f"\r\n--{boundary}--\r\n".encode()
        content_type = f"multipart/form-data; boundary={boundary}"
        response = self._transport.request("POST", self._base_url + "/v2/process/idp/documentParsing",
                                          {"x-api-key": self._api_key, "Content-Type": content_type},
                                          body, self._timeout)
        payload = json.loads(response.body)
        data, code = payload.get("data") or {}, payload.get("code")
        if not (response.status == 200 and str(code) == "200" and
                data.get("status") == "success" and data.get("downloadUrl")):
            fields = [payload.get("msg", payload.get("message", "")),
                      data.get("failureCode", ""), data.get("failureReason", "")]
            msg = " ".join(str(v) for v in fields if isinstance(v, (str, int, float)))
            if str(code) == "06001":
                msg = "免費額度用完 " + msg
            safe_code = code if isinstance(code, (str, int)) else "未知"
            diagnostic = f"ComPDF 回覆失敗：HTTP {response.status} code={safe_code} msg={msg}"
            for sensitive in (body.decode("utf-8", errors="replace"), content_type, "quote-sign/0.1"):
                diagnostic = diagnostic.replace(sensitive, "***")
            raise PdfReadError(self._redact(diagnostic))
        downloaded = self._transport.request("GET", data["downloadUrl"], {}, None, self._timeout)
        if downloaded.status != 200:
            raise PdfReadError(self._redact(f"ComPDF 下載失敗：HTTP {downloaded.status}"))
        content = downloaded.body
        if zipfile.is_zipfile(BytesIO(content)):
            with zipfile.ZipFile(BytesIO(content)) as archive:
                member = next((i for i in archive.infolist()
                               if PurePosixPath(i.filename).name == "result_json.json"), None)
                if member is None or member.file_size > MAX_RESULT_BYTES:
                    raise PdfReadError(self._redact("ComPDF ZIP 缺少結果或結果超過大小限制"))
                with archive.open(member) as source:
                    content = source.read(MAX_RESULT_BYTES + 1)
                if len(content) > MAX_RESULT_BYTES:
                    raise PdfReadError(self._redact("ComPDF 結果超過大小限制"))
        result = json.loads(content)["result"]
        paragraphs, tables, anchors = [], [], {}
        for item in result.get("detail", []):
            text = item.get("text", "").strip()
            if item.get("type") == "table":
                parser = _TableParser()
                parser.feed(text)
                parser.close()
                parser._row_end()
                tables.append(parser.rows)
            else:
                paragraphs.append(text)
                for role, prefix in (("buyer", "買方簽署"), ("seller", "賣方簽署")):
                    if "".join(text.split()).startswith(prefix):
                        pos, page = item.get("position"), item.get("page_id")
                        if (type(page) is int and isinstance(pos, list) and len(pos) == 8
                                and all(type(n) in (int, float) for n in pos)):
                            anchors[role] = (page, (min(pos[::2]), min(pos[1::2]),
                                                    max(pos[::2]), max(pos[1::2])))
        return ParsedPdf(result.get("markdown", ""), paragraphs, tables,
                         result.get("valid_page_number") or result.get("total_page_number") or 1, anchors)
