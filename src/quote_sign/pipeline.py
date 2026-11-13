from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional
import unicodedata
import hashlib
import re

from .checks import (
    EXTRACT_SYSTEM, REVIEW_SYSTEM, build_extract_user, build_review_user,
    check_arithmetic, check_dates, diff_quotes, parse_review, quote_from_compdf,
    quote_from_llm_json, review_notes,
)
from .errors import ExportError, LlmError, PdfReadError, QuoteSignError, SigningError, SigningQuotaExceeded
from .export_pdf import ExportResult
from .models import Issue, LlmClient, LlmResult, PdfReader
from .money import fmt
from .secrets import emit
from .sign import Signer, SignRequest


@dataclass
class Deps:
    export: Callable[[Path, Path], ExportResult]
    reader: Optional[PdfReader]
    llm: Optional[LlmClient]
    signer: Signer
    extract_model: str
    review_model: str
    today: date
    out_dir: Path


def _usage(purpose: str, result: LlmResult) -> None:
    kind = "估算" if result.cost_is_estimate else "實際"
    emit(f"[MixRoute] {purpose} 模型={result.model} 輸入={result.prompt_tokens} "
         f"輸出={result.completion_tokens} 花費=US${result.cost_usd:.6f}（{kind}）秒數={result.seconds:.4f}")


def _stop(issues: list[Issue], review_skipped: bool = False) -> int:
    emit("第 3 步檢查未通過，停止，不送簽：")
    seen = set()
    for issue in issues:
        message = issue.message
        if message not in seen:
            emit("- " + message)
            seen.add(message)
    if review_skipped:
        emit("（條款審查未執行，因為前面已經不過）")
    return 30


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _show_tables(tables: list[list[list[str]]]) -> None:
    emit("ComPDF 讀取的表格：")
    for table in tables:
        # Flatten embedded newlines so that each table row occupies one report line.
        rows = [[" ".join(cell.split()) for cell in row] for row in table]
        columns = max((len(row) for row in rows), default=0)
        widths = [max((_width(row[col]) for row in rows if col < len(row)), default=0)
                  for col in range(columns)]
        for row in rows:
            cells = row + [""] * (columns - len(row))
            emit(" | ".join(cell + " " * (width - _width(cell))
                            for cell, width in zip(cells, widths)).rstrip())


def run(md_path: Path, deps: Deps, send: bool = False, dry_run: bool = False, resend: bool = False) -> int:
    if deps.extract_model == deps.review_model:
        raise QuoteSignError("抽欄位與審條款必須使用不同模型")
    try:
        exported = deps.export(md_path, deps.out_dir)
    except ExportError as exc:
        emit(f"第 1 步（轉 PDF）失敗：{exc}")
        return 10
    emit(f"第 1 步（轉 PDF）完成：{exported.pages} 頁。")
    for warning in exported.diagram_errors:
        emit(f"警告：圖表轉檔異常：{warning}")
    try:
        if deps.reader is None:
            raise PdfReadError("尚未設定 ComPDF 讀取服務")
        parsed = deps.reader.parse(exported.path)
    except PdfReadError as exc:
        emit(f"第 2 步（ComPDF 讀取）失敗：{exc}")
        return 20
    emit(f"第 2 步（ComPDF 讀取）完成：{parsed.page_count} 頁。")
    _show_tables(parsed.tables)
    compdf_q, reading_issues = quote_from_compdf(parsed)
    try:
        if deps.llm is None:
            raise LlmError("尚未設定模型服務")
        extraction = deps.llm.chat_json(
            model=deps.extract_model, purpose="抽欄位", system=EXTRACT_SYSTEM,
            user=build_extract_user(parsed), max_tokens=4096,
        )
        _usage("抽欄位", extraction)
        llm_q = quote_from_llm_json(extraction.data)
    except LlmError as exc:
        emit(f"第 3 步（抽欄位）失敗：{exc}")
        return 31
    issues = (check_arithmetic(compdf_q) + check_dates(compdf_q, deps.today)
              + check_arithmetic(llm_q) + check_dates(llm_q, deps.today)
              + diff_quotes(compdf_q, llm_q, "ComPDF", "模型") + reading_issues)
    if issues:
        return _stop(issues, review_skipped=True)
    try:
        review = deps.llm.chat_json(
            model=deps.review_model, purpose="審條款", system=REVIEW_SYSTEM,
            user=build_review_user(parsed), max_tokens=4096,
        )
        _usage("審條款", review)
        blockers = parse_review(review.data)
    except LlmError as exc:
        emit(f"第 3 步（審條款）失敗：{exc}")
        return 31
    for note in review_notes(review.data):
        emit(f"- 備註：{note}")
    if blockers:
        return _stop(blockers)
    emit("第 1～3 步全部通過。")
    emit(f"品項：{len(compdf_q.items)} 筆。")
    emit(f"未稅合計：NT${fmt(compdf_q.subtotal_total)}")
    emit(f"營業稅：NT${fmt(compdf_q.tax)}")
    emit(f"含稅總計：NT${fmt(compdf_q.total)}")
    if not send:
        emit("未送簽（預設只做到第 3 步；加 --send 才會嘗試送簽）")
        return 0
    try:
        match = re.search(r"報價單\s*(Q\d{4}-\d{3})", parsed.markdown + "\n" + "\n".join(parsed.paragraphs))
        if not match:
            raise SigningError("找不到報價單號") from None
        request = SignRequest(exported.path, hashlib.sha256(md_path.read_bytes()).hexdigest(),
                              match.group(1), compdf_q.buyer, compdf_q.seller,
                              date.fromisoformat(compdf_q.valid_until) if compdf_q.valid_until else None,
                              parsed.anchors.get("buyer"), parsed.anchors.get("seller"))
        deps.signer.send(request, dry_run=dry_run, resend=resend)
    except SigningQuotaExceeded as exc:
        emit(f"第 4 步（送簽）停止：{exc}")
        return 41
    except SigningError as exc:
        emit(f"第 4 步（送簽）停止：{exc}")
        return 40
    return 0
