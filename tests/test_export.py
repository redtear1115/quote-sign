from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from quote_sign.errors import ExportError
from quote_sign.export_pdf import export_pdf
from quote_sign.input_file import InputDoc


def test_success_uses_last_json_line_and_service_output(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert Path(command[2]).read_bytes() == md.data
        return SimpleNamespace(returncode=0, stdout="progress\n" + json.dumps({
            "ok": True, "output": str(tmp_path / "actual.pdf"), "pages": 2,
            "diagramErrors": ["圖表失敗"],
        }) + "\n\n")

    md = InputDoc("quote.md", b"# Fake quote\n")
    out = tmp_path / "nested" / "out"
    result = export_pdf(md, out, runner=runner)
    assert out.is_dir()
    assert result.path == tmp_path / "actual.pdf" and result.pages == 2
    assert result.diagram_errors == ["圖表失敗"]
    private_copy = Path(calls[0][0][2])
    assert private_copy.name == md.name and not private_copy.parent.exists()
    assert calls == [(["marsdawn", "export", str(private_copy), "-o", str(out / "quote.pdf"), "--force", "--json"],
                      {"capture_output": True, "text": True, "timeout": 120})]


def test_nonzero_json_error(tmp_path):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=5, stdout='{"ok":false,"error":"export_failed","message":"x"}')

    with pytest.raises(ExportError, match="export_failed：x"):
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
