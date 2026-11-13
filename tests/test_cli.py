from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest

from quote_sign import cli, secrets
from quote_sign.errors import QuoteSignError


def test_missing_file(tmp_path, monkeypatch, capsys):
    def unexpected(args):
        pytest.fail("Dependencies must not be built for a missing file")

    monkeypatch.setattr(cli, "build_real_deps", unexpected)
    assert cli.main([str(tmp_path / "missing.md")]) == 2
    assert "找不到檔案" in capsys.readouterr().err


def test_arg_parsing(sample_path, deps, tmp_path, monkeypatch):
    captured = []

    def build(args):
        captured.append(args)
        return deps

    monkeypatch.setattr(cli, "build_real_deps", build)
    monkeypatch.setattr(cli, "run", lambda path, deps, send: 40 if send else 0)
    out_dir = tmp_path / "pdf"
    state_dir = tmp_path / "state"
    assert cli.main([str(sample_path), "--out-dir", str(out_dir), "--send", "--today", "2026-11-21",
                     "--extract-model", "extract", "--review-model", "review", "--state-dir", str(state_dir),
                     "--spend-cap-usd", "1.25"]) == 40
    args = captured[0]
    assert args.file == sample_path and args.out_dir == out_dir and args.state_dir == state_dir
    assert args.today == date(2026, 11, 21) and args.spend_cap_usd == Decimal("1.25")
    assert args.extract_model == "extract" and args.review_model == "review" and args.send


def test_defaults(sample_path, deps, monkeypatch):
    captured = []

    def build(args):
        captured.append(args)
        return deps

    monkeypatch.setattr(cli, "build_real_deps", build)
    monkeypatch.setattr(cli, "run", lambda path, deps, send: 0)
    assert cli.main([str(sample_path)]) == 0
    args = captured[0]
    assert args.out_dir == Path("out") and args.state_dir == Path(".quote-sign")
    assert args.extract_model == "deepseek-v4-flash" and args.review_model == "gpt-6-sol"
    assert args.spend_cap_usd == Decimal(5) and not args.send
    assert isinstance(args.today, date)


def test_same_model_config_error(sample_path, monkeypatch, capsys):
    def unexpected(args):
        pytest.fail("Model configuration must be checked before dependency construction")

    monkeypatch.setattr(cli, "build_real_deps", unexpected)
    assert cli.main([str(sample_path), "--extract-model", "same", "--review-model", "same"]) == 2
    assert "不同模型" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["--unknown"], ["q.md", "--today", "bad"],
                                 ["q.md", "--today", "2026-02-30"],
                                 ["q.md", "--spend-cap-usd", "-1"],
                                 ["q.md", "--spend-cap-usd", "NaN"],
                                 ["q.md", "--spend-cap-usd", "Infinity"],
                                 ["q.md", "--spend-cap-usd", "abc"]])
def test_invalid_arguments(args, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2
    assert "參數無效" in capsys.readouterr().err


def test_help_without_secrets(monkeypatch, capsys):
    for name in secrets.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.main(["--help"])
    assert caught.value.code == 0
    output = capsys.readouterr().out
    assert "用法：" in output and "--send" in output and "選項" in output


def test_other_tool_error_returns_one(sample_path, monkeypatch, capsys):
    def build(args):
        raise QuoteSignError("工具無法啟動")

    monkeypatch.setattr(cli, "build_real_deps", build)
    assert cli.main([str(sample_path)]) == 1
    assert "工具無法啟動" in capsys.readouterr().err


def test_module_entrypoint(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["quote-sign", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("quote_sign", run_name="__main__")
    assert caught.value.code == 0
    assert "用法：" in capsys.readouterr().out


def test_lazy_real_client_wiring(sample_path, monkeypatch, tmp_path):
    calls = []
    for name in secrets.SECRET_NAMES:
        monkeypatch.setenv(name, "fake-" + name)

    class ComPdfClient:
        def __init__(self, **kwargs):
            calls.append(("reader", kwargs))

    class SpendLedger:
        def __init__(self, path, **kwargs):
            calls.append(("ledger", path, kwargs))

    class MixRouteClient:
        def __init__(self, **kwargs):
            calls.append(("llm", kwargs))

    compdf = ModuleType("quote_sign.compdf")
    compdf.ComPdfClient = ComPdfClient
    llm = ModuleType("quote_sign.llm")
    llm.MixRouteClient = MixRouteClient
    llm.SpendLedger = SpendLedger
    monkeypatch.setitem(sys.modules, "quote_sign.compdf", compdf)
    monkeypatch.setitem(sys.modules, "quote_sign.llm", llm)
    args = type("Args", (), {
        "state_dir": tmp_path, "spend_cap_usd": Decimal("2.5"), "out_dir": tmp_path / "out",
        "extract_model": "cheap", "review_model": "strong", "today": date(2026, 11, 20),
    })()
    deps = cli.build_real_deps(args)
    assert isinstance(deps.reader, ComPdfClient) and isinstance(deps.llm, MixRouteClient)
    assert calls[0] == ("reader", {"api_key": "fake-COMPDF_PUBLIC_KEY", "transport": None})
    assert calls[1] == ("ledger", tmp_path / "spend.json", {"cap_usd": Decimal("2.5")})
    assert calls[2][1]["base_url"] == "fake-MIX_ROUTE_BASE_URL"
    assert calls[2][1]["api_key"] == "fake-MIX_ROUTE_API_KEY"
    assert calls[2][1]["transport"] is None


def test_real_client_construction(monkeypatch, tmp_path):
    from quote_sign.compdf import ComPdfClient
    from quote_sign.llm import MixRouteClient
    from quote_sign.pipeline import Deps

    for name, value in {
        "MIX_ROUTE_API_KEY": "fake-mixroute-key",
        "MIX_ROUTE_BASE_URL": "https://mix.example/v1",
        "COMPDF_PUBLIC_KEY": "fake-compdf-public",
        "COMPDF_SECRET_KEY": "fake-compdf-unused-secret",
    }.items():
        monkeypatch.setenv(name, value)
    args = type("Args", (), {
        "state_dir": tmp_path / "state", "spend_cap_usd": Decimal("5"),
        "out_dir": tmp_path / "out", "today": date(2026, 11, 13),
        "extract_model": "deepseek-v4-flash", "review_model": "gpt-6-sol",
    })()
    deps = cli.build_real_deps(args)
    assert isinstance(deps, Deps)
    assert isinstance(deps.reader, ComPdfClient) and isinstance(deps.llm, MixRouteClient)
    assert deps.llm.ledger.path == args.state_dir / "spend.json"
    assert not args.state_dir.exists()
