from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest

from quote_sign import cli, secrets
from quote_sign.errors import MissingSecretError


@pytest.fixture
def fake_keys(monkeypatch):
    values = {name: f"fake-key-{index}-AAAA1111BBBB" for index, name in enumerate(secrets.SECRET_NAMES)}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_mask_all_keys_at_call_time(fake_keys, monkeypatch):
    combined = " | ".join(fake_keys.values())
    masked = secrets.mask(combined)
    assert masked.count("***") == len(fake_keys)
    assert not any(value in masked for value in fake_keys.values())
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "new-key-CCCC2222")
    assert secrets.mask("new-key-CCCC2222") == "***"


def test_mask_non_string(fake_keys):
    assert "fake-key" not in secrets.mask(list(fake_keys.values()))
    assert secrets.mask(123) == "123"


def test_mask_short_and_empty_values(monkeypatch):
    for name in secrets.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "abc")
    assert secrets.mask("abc def") == "abc def"
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "abcd")
    assert secrets.mask("abcd abcd") == "*** ***"


def test_mask_overlapping_values(monkeypatch):
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "abcd")
    monkeypatch.setenv("COMPDF_SECRET_KEY", "abcd-efgh")
    assert secrets.mask("abcd-efgh abcd") == "*** ***"


def test_help_does_not_expose_keys(fake_keys, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["--help"])
    assert caught.value.code == 0
    captured = capsys.readouterr()
    assert not any(value in captured.out + captured.err for value in fake_keys.values())


def test_missing_file_has_no_secrets(fake_keys, tmp_path, capsys):
    path = tmp_path / fake_keys["MIX_ROUTE_API_KEY"] / "missing.md"
    assert cli.main([str(path)]) == 2
    captured = capsys.readouterr()
    assert not any(value in captured.out + captured.err for value in fake_keys.values())
    assert "***" in captured.err


def test_unexpected_client_exception_is_masked(fake_keys, deps, sample_path, monkeypatch, capsys):
    deps.llm.error = RuntimeError("service failed: " + fake_keys["MIX_ROUTE_API_KEY"])
    monkeypatch.setattr(cli, "build_real_deps", lambda args: deps)
    assert cli.main([str(sample_path)]) == 70
    captured = capsys.readouterr()
    assert "未預期的錯誤：RuntimeError" in captured.err
    assert "***" in captured.err
    assert not any(value in captured.out + captured.err for value in fake_keys.values())
    assert "Traceback" not in captured.err


def test_require_reports_only_missing_names(monkeypatch):
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "fake-present-value-1234")
    monkeypatch.delenv("COMPDF_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("COMPDF_SECRET_KEY", "")
    with pytest.raises(MissingSecretError) as caught:
        secrets.require("MIX_ROUTE_API_KEY", "COMPDF_PUBLIC_KEY", "COMPDF_SECRET_KEY")
    assert caught.value.names == ["COMPDF_PUBLIC_KEY", "COMPDF_SECRET_KEY"]
    assert "COMPDF_PUBLIC_KEY" in str(caught.value)
    assert "COMPDF_SECRET_KEY" in str(caught.value)
    assert "fake-present-value-1234" not in str(caught.value)
    assert secrets.require("MIX_ROUTE_API_KEY") == {"MIX_ROUTE_API_KEY": "fake-present-value-1234"}


def test_default_cli_missing_env_reports_names_only(sample_path, monkeypatch, capsys):
    for name in secrets.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert cli.main([str(sample_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("缺少環境變數：MIX_ROUTE_API_KEY, MIX_ROUTE_BASE_URL, "
                            "COMPDF_PUBLIC_KEY, COMPDF_SECRET_KEY（只列名稱）\n")


def test_log_masking(fake_keys):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        secrets.install_log_masking()
        logging.getLogger("quote_sign.test.child").warning("key=%s", fake_keys["MIX_ROUTE_API_KEY"])
        assert fake_keys["MIX_ROUTE_API_KEY"] not in stream.getvalue()
        assert "key=***" in stream.getvalue()
    finally:
        root.removeHandler(handler)


def test_exception_log_masking(fake_keys):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        secrets.install_log_masking()
        try:
            raise RuntimeError(fake_keys["COMPDF_SECRET_KEY"])
        except RuntimeError:
            logging.getLogger("quote_sign.test.child").exception("request failed")
        assert fake_keys["COMPDF_SECRET_KEY"] not in stream.getvalue()
        assert "RuntimeError: ***" in stream.getvalue()
    finally:
        root.removeHandler(handler)


def test_log_install_is_idempotent():
    root = logging.getLogger()
    secrets.install_log_masking()
    before = len(root.filters)
    secrets.install_log_masking()
    assert len(root.filters) == before


def test_only_secrets_module_reads_environment_and_prints():
    source = Path(__file__).resolve().parents[1] / "src" / "quote_sign"
    for path in source.rglob("*.py"):
        if path.name == "secrets.py":
            continue
        text = path.read_text()
        assert "os.environ" not in text, path
        assert "getenv" not in text, path
        assert "print(" not in text, path


def test_disabling_masking_proves_value_would_leak(fake_keys, monkeypatch, capsys):
    monkeypatch.setattr(secrets, "MASKING_ENABLED", False)
    value = fake_keys["MIX_ROUTE_API_KEY"]
    assert secrets.mask(value) == value
    secrets.emit(value)
    assert value in capsys.readouterr().out


def test_stdout_and_stderr_are_masked(fake_keys, capsys):
    value = fake_keys["COMPDF_SECRET_KEY"]
    secrets.emit(value)
    secrets.emit_err(value)
    captured = capsys.readouterr()
    assert captured.out == "***\n" and captured.err == "***\n"
