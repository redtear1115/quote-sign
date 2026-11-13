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


def test_smart_routing_key_is_a_masked_secret(fake_keys):
    assert "MIX_ROUTE_SMART_ROUTING_KEY" in secrets.SECRET_NAMES
    assert secrets.mask(fake_keys["MIX_ROUTE_SMART_ROUTING_KEY"]) == "***"


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


@pytest.mark.parametrize("name", ["MIX_ROUTE_API_KEY", "MIX_ROUTE_SMART_ROUTING_KEY"])
def test_unexpected_client_exception_is_masked(name, fake_keys, deps, sample_path, monkeypatch, capsys):
    deps.llm.error = RuntimeError("service failed: " + fake_keys[name])
    monkeypatch.setattr(cli, "build_real_deps", lambda args: deps)
    assert cli.main([str(sample_path)]) == 70
    captured = capsys.readouterr()
    assert "未預期的錯誤：RuntimeError" in captured.err
    assert captured.err == "未預期的錯誤：RuntimeError\n"
    assert "service failed" not in captured.err
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
    assert captured.err == ("缺少環境變數：MIX_ROUTE_SMART_ROUTING_KEY, MIX_ROUTE_API_KEY, "
                            "MIX_ROUTE_BASE_URL, COMPDF_PUBLIC_KEY, COMPDF_SECRET_KEY（只列名稱）\n")


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


VALID_MASK_EMAIL_CASES = [
    ("alice@example.invalid", "a***@e***.invalid"),
    ("bob@mail.example.invalid", "***@m***.invalid"),
    ("a@b.example.invalid", "***@***.invalid"),
    ("ab@cd.example.invalid", "***@***.invalid"),
    ("abc@def.example.invalid", "***@***.invalid"),
    ("abcd@efgh.example.invalid", "a***@e***.invalid"),
    ("buyer@example.invalid", "b***@e***.invalid"),
    ("longlocalpart@longdomain.example.invalid", "l***@l***.invalid"),
    ("用戶名稱@example.invalid", "用***@e***.invalid"),
]


@pytest.mark.parametrize("addr,expected", VALID_MASK_EMAIL_CASES + [
    ("", "***"), (None, "***"), (123, "***"),
    ("invalid", "***"), ("@example.invalid", "***"), ("a@", "***"),
    ("a@example", "***"), ("a@@example.invalid", "***"), ("a@.invalid", "***"),
    ("a b@example.invalid", "***")])
def test_mask_email(addr, expected):
    assert secrets.mask_email(addr) == expected


@pytest.mark.parametrize("addr,expected", VALID_MASK_EMAIL_CASES)
def test_short_email_parts_are_fully_hidden(addr, expected):
    assert secrets._valid_email(addr)
    local, domain = addr.split("@")
    masked_local, masked_domain = secrets.mask_email(addr).split("@")
    first_label = domain.split(".")[0]
    if len(local) < 4:
        assert masked_local == "***" and local not in masked_local
    if len(first_label) < 4:
        assert masked_domain.split(".")[0] == "***"
        assert first_label not in masked_domain.split(".")[0]


def test_email_masking_case_insensitive_and_arbitrary(monkeypatch):
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", "buyer@example.invalid")
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", "seller@example.invalid")
    monkeypatch.setenv("MIX_ROUTE_API_KEY", "fake-secret-0001")
    output = secrets.mask("BUYER@EXAMPLE.INVALID seller@example.invalid other@else.invalid fake-secret-0001")
    assert output == "b***@e***.invalid s***@e***.invalid o***@e***.invalid ***"


def test_email_reverse_check(monkeypatch, capsys):
    value = "buyer@example.invalid"
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", value)
    monkeypatch.setattr(secrets, "MASKING_ENABLED", False)
    secrets.emit(value)
    assert capsys.readouterr().out == value + "\n"


@pytest.mark.parametrize("buyer,seller", [("bad", "seller@example.invalid"),
    ("buyer@example.invalid", "BUYER@EXAMPLE.INVALID"), ("a b@example.invalid", "seller@example.invalid"),
    ("a@@example.invalid", "seller@example.invalid"), ("a@nodot", "seller@example.invalid"),
    ("a"*250+"@x.invalid", "seller@example.invalid")])
def test_invalid_signer_configuration_names_only(monkeypatch, buyer, seller):
    from quote_sign.errors import SigningNotConfigured
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", buyer)
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", seller)
    with pytest.raises(SigningNotConfigured) as caught:
        secrets.require_signer_emails()
    assert buyer not in str(caught.value) and seller not in str(caught.value)
    assert "QUOTE_SIGN_BUYER_EMAIL" in str(caught.value)


def test_require_signers_missing_then_valid(monkeypatch):
    with pytest.raises(MissingSecretError) as caught:
        secrets.require_signer_emails()
    assert caught.value.names == list(secrets.EMAIL_NAMES)
    values = ("buyer@example.invalid", "seller@example.invalid")
    for name, value in zip(secrets.EMAIL_NAMES, values):
        monkeypatch.setenv(name, value)
    assert secrets.require_signer_emails() == values
