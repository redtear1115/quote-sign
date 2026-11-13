from __future__ import annotations

from decimal import Decimal

import pytest

from quote_sign.errors import BudgetExceeded, ExportError, LlmError, PdfReadError, QuoteSignError
from quote_sign.pipeline import run
from quote_sign.sign import RefusingSigner


def test_all_good(deps, sample_path, capsys):
    assert run(sample_path, deps) == 0
    assert [c["model"] for c in deps.llm.calls] == ["cheap", "strong"]
    assert [c["purpose"] for c in deps.llm.calls] == ["抽欄位", "審條款"]
    assert all(c["user"] == deps.reader.parsed.markdown for c in deps.llm.calls)
    assert deps.reader.calls == [deps.out_dir / "from-export.pdf"]
    assert not deps.signer.calls
    output = capsys.readouterr().out
    assert "第 1～3 步全部通過。" in output
    assert "未送簽" in output and "44,730" in output and "品項：3 筆" in output


def test_bad_sample_stops_before_review_or_send(deps, sample_path, capsys):
    bad_path = sample_path.with_name("quote-Q2026-031-bad.md")
    good = sample_path.read_text()
    bad = bad_path.read_text()
    assert bad == good.replace("| 自訂主題設計 | 1 | 15,000 | 15,000 |",
                               "| 自訂主題設計 | 1 | 15,000 | 12,000 |")
    deps.reader.parsed.tables[0][2][3] = "12,000"
    deps.llm.extraction["items"][1]["subtotal"] = "12,000"
    assert run(bad_path, deps, send=True) == 30
    output = capsys.readouterr().out
    assert output.count("第 2 列小計應為 15,000") == 1
    assert "條款審查未執行" in output
    assert "[第 2 列]" not in output
    assert len(deps.llm.calls) == 1
    assert not deps.signer.calls


def test_readers_disagree(deps, sample_path, capsys):
    deps.llm.extraction["items"][1]["subtotal"] = "12000"
    assert run(sample_path, deps, send=True) == 30
    assert "第 2 列小計：ComPDF 讀到 15,000，模型讀到 12,000" in capsys.readouterr().out
    assert len(deps.llm.calls) == 1 and not deps.signer.calls


def test_compdf_side_is_also_checked(deps, sample_path, capsys):
    deps.reader.parsed.tables[0][2][3] = "12,000"
    assert run(sample_path, deps, send=True) == 30
    output = capsys.readouterr().out
    assert "第 2 列小計應為 15,000" in output
    assert "ComPDF 讀到 12,000，模型讀到 15,000" in output
    assert len(deps.llm.calls) == 1 and not deps.signer.calls


def test_blocking_review(deps, sample_path, capsys):
    deps.llm.review = {"verdict": "fail", "findings": [
        {"clause": "付款條件", "severity": "blocker", "reason": "付款期限互相矛盾。"},
    ]}
    assert run(sample_path, deps, send=True) == 30
    assert "付款條件：付款期限互相矛盾。" in capsys.readouterr().out
    assert len(deps.llm.calls) == 2 and not deps.signer.calls


@pytest.mark.parametrize("data", [{}, {"verdict": "fail", "findings": []},
                                 {"verdict": "pass", "findings": [None]}])
def test_bad_review_fails_closed(deps, sample_path, data):
    deps.llm.review = data
    assert run(sample_path, deps, send=True) == 30
    assert not deps.signer.calls


def test_notes_do_not_block(deps, sample_path, capsys):
    deps.llm.review = {"verdict": "pass", "findings": [
        {"clause": "付款條件", "severity": "note", "reason": "建議註明付款方式。"},
    ]}
    assert run(sample_path, deps) == 0
    assert "- 備註：付款條件：建議註明付款方式。" in capsys.readouterr().out


@pytest.mark.parametrize("purpose", ["抽欄位", "審條款"])
@pytest.mark.parametrize("error", [LlmError("模型失敗"), BudgetExceeded("超過花費上限")])
def test_model_error(deps, sample_path, capsys, purpose, error):
    deps.llm.error = error
    deps.llm.error_purpose = purpose
    assert run(sample_path, deps, send=True) == 31
    assert f"第 3 步（{purpose}）失敗" in capsys.readouterr().out
    assert not deps.signer.calls
    assert len(deps.llm.calls) == (1 if purpose == "抽欄位" else 2)


def test_export_error(deps, sample_path, capsys):
    deps.export.error = ExportError("轉檔失敗")
    assert run(sample_path, deps, send=True) == 10
    assert "第 1 步（轉 PDF）失敗" in capsys.readouterr().out
    assert not deps.reader.calls and not deps.llm.calls and not deps.signer.calls


def test_pdf_error(deps, sample_path, capsys):
    deps.reader.error = PdfReadError("讀取失敗")
    assert run(sample_path, deps, send=True) == 20
    assert "第 2 步（ComPDF 讀取）失敗" in capsys.readouterr().out
    assert not deps.llm.calls and not deps.signer.calls


def test_send_refused(deps, sample_path, capsys):
    deps.signer = RefusingSigner()
    assert run(sample_path, deps, send=True) == 40
    output = capsys.readouterr().out
    assert "第 4 步（送簽）停止" in output
    assert "QUOTE_SIGN_TEST_EMAIL_1 / QUOTE_SIGN_TEST_EMAIL_2" in output


def test_signer_only_called_after_pass_with_placeholder_emails(deps, sample_path):
    assert run(sample_path, deps, send=True) == 0
    assert deps.signer.calls == [((deps.out_dir / "from-export.pdf",),
                                 {"buyer_email": "", "seller_email": "", "dry_run": True})]


def test_same_models_fail_before_any_call(deps, sample_path):
    deps.review_model = deps.extract_model
    with pytest.raises(QuoteSignError, match="不同模型"):
        run(sample_path, deps, send=True)
    assert not deps.export.calls and not deps.reader.calls
    assert not deps.llm.calls and not deps.signer.calls


def test_every_call_reports_actual_model_and_cost(deps, sample_path, capsys):
    assert run(sample_path, deps) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[MixRoute]")]
    assert len(lines) == 2
    assert "模型=actual-extractor" in lines[0] and "估算" in lines[0]
    assert "模型=actual-reviewer" in lines[1] and "實際" in lines[1]
    assert all("US$0.001234" in line and "輸入=100 輸出=50" in line and "秒數=0.1250" in line
               for line in lines)


def test_diagram_warning(deps, sample_path, capsys):
    deps.export.warnings = ["第 1 張圖表無法繪製"]
    assert run(sample_path, deps) == 0
    assert "警告：圖表轉檔異常：第 1 張圖表無法繪製" in capsys.readouterr().out


def test_expired_date_stops_review(deps, sample_path):
    from datetime import date

    deps.today = date(2026, 12, 6)
    assert run(sample_path, deps, send=True) == 30
    assert len(deps.llm.calls) == 1 and not deps.signer.calls


def test_missing_reader(deps, sample_path):
    deps.reader = None
    assert run(sample_path, deps) == 20
    assert not deps.llm.calls


def test_missing_llm(deps, sample_path):
    deps.llm = None
    assert run(sample_path, deps) == 31


def test_unreadable_cell_stops_review(deps, sample_path, capsys):
    deps.reader.parsed.tables[0][2][2] = "?"
    deps.llm.extraction["items"][1]["unit_price"] = None
    assert run(sample_path, deps, send=True) == 30
    assert "第 2 列單價無法讀取" in capsys.readouterr().out
    assert len(deps.llm.calls) == 1 and not deps.signer.calls


def test_report_table_rows_align(deps, sample_path, capsys):
    assert run(sample_path, deps) == 0
    rows = [line for line in capsys.readouterr().out.splitlines() if " | " in line]
    assert len(rows) == 4
    from quote_sign.pipeline import _width

    assert len({_width(row.split(" | ")[0]) for row in rows}) == 1
