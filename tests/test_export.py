from __future__ import annotations

import errno
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from quote_sign.errors import ExportError
from quote_sign.export_pdf import export_pdf
from quote_sign.input_file import InputDoc


def test_success_uses_last_json_line_and_private_output(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert Path(command[2]).read_bytes() == md.data
        Path(command[4]).write_bytes(b"%PDF-fake")
        return SimpleNamespace(returncode=0, stdout="progress\n" + json.dumps({
            "ok": True, "output": str(tmp_path / "actual.pdf"), "pages": 2,
            "diagramErrors": ["圖表失敗"],
        }) + "\n\n")

    md = InputDoc("quote.md", b"# Fake quote\n")
    out = tmp_path / "nested" / "out"
    result = export_pdf(md, out, runner=runner)
    assert out.is_dir()
    assert result.path == out / "quote.pdf" and result.pages == 2
    assert result.path.read_bytes() == b"%PDF-fake"
    assert not (tmp_path / "actual.pdf").exists()
    assert result.diagram_errors == ["圖表失敗"]
    private_copy = Path(calls[0][0][2])
    assert private_copy.name == md.name and not private_copy.parent.exists()
    assert calls == [(["marsdawn", "export", str(private_copy), "-o", str(private_copy.with_suffix(".pdf")), "--force", "--json"],
                      {"capture_output": True, "text": True, "timeout": 120})]


def test_nonzero_json_error(tmp_path):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=5, stdout='{"ok":false,"error":"export_failed","message":"x"}')

    with pytest.raises(ExportError, match="export_failed：轉檔失敗"):
        export_pdf(InputDoc("q.md", b"# Fake quote\n"), tmp_path, runner=runner)


def test_missing_binary(tmp_path):
    def runner(*args, **kwargs):
        raise FileNotFoundError("marsdawn")

    with pytest.raises(ExportError, match="brew install redtear1115/tap/marsdawn"):
        export_pdf(InputDoc("q.md", b"# Fake quote\n"), tmp_path, runner=runner)


@pytest.mark.parametrize("code,stdout", [(0, ""), (0, "not JSON"), (0, "[]"),
                                       (0, '{"ok":true}'), (0, '{"ok":false}'),
                                       (5, '{"ok":true,"output":"x.pdf"}'),
                                       (0, '{"ok":1,"output":"x.pdf"}')])
def test_invalid_or_unsuccessful_response(tmp_path, code, stdout):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=code, stdout=stdout)

    with pytest.raises(ExportError):
        export_pdf(InputDoc("q.md", b"# Fake quote\n"), tmp_path, runner=runner)


def test_timeout(tmp_path):
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("marsdawn", 120)

    with pytest.raises(ExportError):
        export_pdf(InputDoc("q.md", b"# Fake quote\n"), tmp_path, runner=runner)


@pytest.mark.parametrize("code,kind", [(2, "input_not_found"), (4, "output_exists"),
                                    (5, "export_failed"), (64, "usage")])
def test_exit_code_fallback(tmp_path, code, kind):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=code, stdout='{"ok":false}')

    with pytest.raises(ExportError, match=kind):
        export_pdf(InputDoc("q.md", b"# Fake quote\n"), tmp_path, runner=runner)


@pytest.fixture
def export_scratch(monkeypatch):
    import shutil
    import tempfile
    directory = tempfile.mkdtemp(dir="/tmp")
    try:
        monkeypatch.setattr(tempfile, "tempdir", directory)
        yield Path(directory)
    finally:
        shutil.rmtree(directory)


def pdf_runner(command, **kwargs):
    Path(command[4]).write_bytes(b"%PDF-fake")
    return SimpleNamespace(returncode=0, stdout='{"ok":true,"pages":1}')


def test_output_creation_error_is_fixed_and_redacted(export_scratch):
    parent = export_scratch / "file"
    parent.write_bytes(b"fake")
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), parent / "out", pdf_runner)
    assert str(caught.value) == "輸出目錄不符規則（不是目錄）"
    assert str(export_scratch) not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("number,message", [
    (errno.ENOSPC, "轉檔無法完成：磁碟空間不足"),
    (errno.EDQUOT, "轉檔無法完成：磁碟空間不足"),
    (errno.EACCES, "轉檔無法完成：權限不足"),
    (errno.EPERM, "轉檔無法完成：權限不足"),
    (errno.EROFS, "轉檔無法完成：權限不足"),
    (errno.EIO, "轉檔無法完成：檔案系統錯誤"),
])
@pytest.mark.parametrize("stage", ["snapshot", "publish"])
def test_write_errors_are_redacted_and_cleaned(export_scratch, monkeypatch, number, message, stage):
    import importlib
    module = importlib.import_module("quote_sign.export_pdf")
    out = export_scratch / "out"
    private_dirs = []
    def fail(*args):
        raise OSError(number, "No space CANARY-QUOTE-BODY-7f3a", "/secret/tmp/path")
    def runner(command, **kwargs):
        private_dirs.append(Path(command[2]).parent)
        result = pdf_runner(command, **kwargs)
        monkeypatch.setattr(module.os, "write", fail)
        return result
    if stage == "snapshot":
        monkeypatch.setattr(module.os, "write", fail)
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), out, runner)
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    assert "/secret" not in str(caught.value)
    assert str(export_scratch) not in str(caught.value)
    assert not list(out.glob(".quote-sign-*.tmp"))
    assert not list(export_scratch.glob("quote-sign-src-*"))
    assert all(not path.exists() for path in private_dirs)


@pytest.mark.parametrize("kind,expected", [
    ("input_not_found", "input_not_found"), ("output_exists", "output_exists"),
    ("export_failed", "export_failed"), ("usage", "usage"),
    ("timeout", "timeout"), ("invalid_input", "invalid_input"),
    ("CANARY-QUOTE-BODY-7f3a", "export_failed"),
    ({"CANARY-QUOTE-BODY-7f3a": True}, "export_failed"),
])
def test_failure_payload_is_allowlisted(export_scratch, kind, expected):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=5, stdout=json.dumps({
            "ok": False, "error": kind, "message": "CANARY-QUOTE-BODY-7f3a /secret/path",
        }))
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), export_scratch / "out", runner)
    assert str(caught.value) == expected + "：轉檔失敗"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("kind", ["missing", "text", "directory", "symlink", "fifo", "oversize"])
def test_invalid_known_output_is_refused(export_scratch, kind):
    import os
    def runner(command, **kwargs):
        path = Path(command[4])
        if kind == "text":
            path.write_bytes(b"CANARY-QUOTE-BODY-7f3a")
        elif kind == "directory":
            path.mkdir()
        elif kind == "symlink":
            target = export_scratch / "elsewhere.pdf"
            target.write_bytes(b"%PDF-fake")
            path.symlink_to(target)
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "oversize":
            with path.open("wb") as output:
                output.write(b"%PDF-fake")
                output.truncate(100 * 1024 * 1024 + 1)
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"output":"ignored"}')
    out = export_scratch / "out"
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), out, runner)
    assert str(caught.value) == "marsdawn 的輸出無效"
    assert caught.value.__cause__ is None
    assert not list(out.iterdir())


def test_private_output_is_required_even_if_payload_output_exists(export_scratch):
    other = export_scratch / "other.pdf"
    other.write_bytes(b"%PDF-elsewhere")
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "output": str(other)}))
    with pytest.raises(ExportError, match="marsdawn 的輸出無效"):
        export_pdf(InputDoc("q.md", b"fake"), export_scratch / "out", runner)
    assert other.read_bytes() == b"%PDF-elsewhere"


def test_final_symlink_is_replaced_and_output_is_private(export_scratch):
    import stat
    out = export_scratch / "out"
    out.mkdir()
    target = export_scratch / "target.pdf"
    target.write_bytes(b"unchanged")
    final = out / "q.pdf"
    final.symlink_to(target)
    result = export_pdf(InputDoc("q.md", b"fake"), out, pdf_runner)
    assert result.path == final
    assert not final.is_symlink()
    assert final.read_bytes() == b"%PDF-fake"
    assert target.read_bytes() == b"unchanged"
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert not list(out.glob(".quote-sign-*.tmp"))


def test_post_preflight_directory_symlink_is_rejected(export_scratch):
    from quote_sign.safe_paths import check_out_dir
    out = export_scratch / "out"
    target = export_scratch / "target"
    target.mkdir()
    check_out_dir(out)
    out.symlink_to(target, target_is_directory=True)
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), out, pdf_runner)
    assert str(caught.value) == "輸出目錄不符規則（路徑含符號連結）"
    assert caught.value.__cause__ is None
    assert not list(target.iterdir())


def test_open_output_descriptor_survives_directory_substitution(export_scratch):
    out = export_scratch / "out"
    target = export_scratch / "target"
    target.mkdir()
    pinned = export_scratch / "pinned"
    def runner(command, **kwargs):
        out.rename(pinned)
        out.symlink_to(target, target_is_directory=True)
        return pdf_runner(command, **kwargs)
    export_pdf(InputDoc("q.md", b"fake"), out, runner)
    assert (pinned / "q.pdf").read_bytes() == b"%PDF-fake"
    assert not list(target.iterdir())


@pytest.mark.parametrize("exception,message", [
    (subprocess.TimeoutExpired("/secret/path", 120), "轉檔逾時"),
    (subprocess.SubprocessError("CANARY-QUOTE-BODY-7f3a"), "轉檔無法完成"),
])
def test_subprocess_errors_have_fixed_messages(export_scratch, exception, message):
    def runner(*args, **kwargs):
        raise exception
    with pytest.raises(ExportError) as caught:
        export_pdf(InputDoc("q.md", b"fake"), export_scratch / "out", runner)
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
