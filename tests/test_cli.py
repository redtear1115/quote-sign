from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest

from quote_sign import cli, paths, secrets
from quote_sign.errors import QuoteSignError
from quote_sign.input_file import InputDoc
from quote_sign.llm import PurposeRoutedClient


def test_missing_file(tmp_path, monkeypatch, capsys):
    def unexpected(args):
        pytest.fail("Dependencies must not be built for a missing file")

    monkeypatch.setattr(cli, "build_real_deps", unexpected)
    assert cli.main([str(tmp_path / "missing.md")]) == 2
    assert "找不到檔案" in capsys.readouterr().err


def test_arg_parsing(sample_path, deps, tmp_path, monkeypatch):
    captured = []
    sources = []

    def build(args):
        captured.append(args)
        return deps

    monkeypatch.setattr(cli, "build_real_deps", build)
    def run(doc, deps, send, **kwargs):
        sources.append(doc)
        return 40 if send else 0
    monkeypatch.setattr(cli, "run", run)
    out_dir = tmp_path / "pdf"
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", "buyer@example.invalid")
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", "seller@example.invalid")
    for name in cli.DOTTEDSIGN_NAMES:
        monkeypatch.setenv(name, "fake-dottedsign-credential-0001")
    assert cli.main([str(sample_path), "--out-dir", str(out_dir), "--send", "--today", "2026-11-21",
                     "--extract-model", "extract", "--review-model", "review",
                     "--spend-cap-usd", "1.25"]) == 40
    args = captured[0]
    assert sources == [InputDoc(sample_path.name, sample_path.read_bytes())]
    assert args.file == sample_path and args.out_dir == out_dir
    assert args.today == date(2026, 11, 21) and args.spend_cap_usd == Decimal("1.25")
    assert args.extract_model == "extract" and args.review_model == "review" and args.send


def test_defaults(sample_path, deps, monkeypatch):
    captured = []

    def build(args):
        captured.append(args)
        return deps

    monkeypatch.setattr(cli, "build_real_deps", build)
    monkeypatch.setattr(cli, "run", lambda path, deps, send, **kwargs: 0)
    assert cli.main([str(sample_path)]) == 0
    args = captured[0]
    assert args.out_dir == Path("out")
    assert args.extract_model == "auto" and args.review_model == "gpt-6-sol"
    assert args.spend_cap_usd == Decimal(5) and not args.send
    assert isinstance(args.today, date)


def test_same_model_config_error(sample_path, monkeypatch, capsys):
    def unexpected(args):
        pytest.fail("Model configuration must be checked before dependency construction")

    monkeypatch.setattr(cli, "build_real_deps", unexpected)
    assert cli.main([str(sample_path), "--extract-model", "same", "--review-model", "same"]) == 2
    assert "不同模型" in capsys.readouterr().err


def test_auto_and_pinned_review_model_is_valid(sample_path, deps, monkeypatch):
    monkeypatch.setattr(cli, "build_real_deps", lambda args: deps)
    monkeypatch.setattr(cli, "run", lambda path, deps, send: 0)
    assert cli.main([str(sample_path), "--extract-model", "auto", "--review-model", "gpt-6-sol"]) == 0


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

        def check(self):
            calls.append(("check",))

    class MixRouteClient:
        def __init__(self, **kwargs):
            calls.append(("llm", kwargs))
            self.kwargs = kwargs

    compdf = ModuleType("quote_sign.compdf")
    compdf.ComPdfClient = ComPdfClient
    llm = ModuleType("quote_sign.llm")
    llm.MixRouteClient = MixRouteClient
    llm.SpendLedger = SpendLedger
    llm.PurposeRoutedClient = PurposeRoutedClient
    monkeypatch.setitem(sys.modules, "quote_sign.compdf", compdf)
    monkeypatch.setitem(sys.modules, "quote_sign.llm", llm)
    args = type("Args", (), {
        "spend_cap_usd": Decimal("2.5"), "out_dir": tmp_path / "out",
        "extract_model": "auto", "review_model": "strong", "today": date(2026, 11, 20),
    })()
    deps = cli.build_real_deps(args)
    assert isinstance(deps.reader, ComPdfClient) and isinstance(deps.llm, PurposeRoutedClient)
    assert isinstance(deps.llm.extract, MixRouteClient) and isinstance(deps.llm.review, MixRouteClient)
    assert deps.llm.extract is not deps.llm.review
    assert sorted(call[0] for call in calls) == ["check", "ledger", "llm", "llm", "reader"]
    assert ("ledger", Path.home() / ".quote-sign" / "spend.json", {"cap_usd": Decimal("2.5")}) in calls
    assert ("check",) in calls
    assert ("reader", {"api_key": "fake-COMPDF_PUBLIC_KEY", "transport": None}) in calls
    extract, review = deps.llm.extract.kwargs, deps.llm.review.kwargs
    assert extract["api_key"] == "fake-MIX_ROUTE_SMART_ROUTING_KEY"
    assert review["api_key"] == "fake-MIX_ROUTE_API_KEY"
    assert extract["base_url"] == review["base_url"] == "fake-MIX_ROUTE_BASE_URL"
    assert extract["transport"] is None and review["transport"] is None
    assert extract["ledger"] is review["ledger"]
    assert (deps.extract_model, deps.review_model) == ("auto", "strong")


def test_missing_smart_routing_key_reports_only_its_name(sample_path, monkeypatch, capsys):
    for name in secrets.SECRET_NAMES:
        monkeypatch.setenv(name, "fake-" + name)
    monkeypatch.delenv("MIX_ROUTE_SMART_ROUTING_KEY")
    assert cli.main([str(sample_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "缺少環境變數：MIX_ROUTE_SMART_ROUTING_KEY（只列名稱）\n"


def test_real_client_construction(monkeypatch, tmp_path):
    from quote_sign.compdf import ComPdfClient
    from quote_sign.llm import MixRouteClient
    from quote_sign.pipeline import Deps

    for name, value in {
        "MIX_ROUTE_SMART_ROUTING_KEY": "fake-smart-routing-key",
        "MIX_ROUTE_API_KEY": "fake-mixroute-key",
        "MIX_ROUTE_BASE_URL": "https://mix.example/v1",
        "COMPDF_PUBLIC_KEY": "fake-compdf-public",
        "COMPDF_SECRET_KEY": "fake-compdf-unused-secret",
    }.items():
        monkeypatch.setenv(name, value)
    args = type("Args", (), {
        "spend_cap_usd": Decimal("5"),
        "out_dir": tmp_path / "out", "today": date(2026, 11, 13),
        "extract_model": "auto", "review_model": "gpt-6-sol",
    })()
    deps = cli.build_real_deps(args)
    assert isinstance(deps, Deps)
    assert isinstance(deps.reader, ComPdfClient) and isinstance(deps.llm, PurposeRoutedClient)
    assert isinstance(deps.llm.extract, MixRouteClient) and isinstance(deps.llm.review, MixRouteClient)
    assert deps.llm.extract._api_key == "fake-smart-routing-key"
    assert deps.llm.review._api_key == "fake-mixroute-key"
    assert deps.llm.extract.ledger is deps.llm.review.ledger
    assert deps.llm.extract.ledger.path == Path.home() / ".quote-sign" / "spend.json"


@pytest.mark.parametrize("arguments", [["--spend-cap-usd", "5.01"],
                                       ["--dry-run"], ["--resend"]])
def test_new_usage_errors(sample_path, arguments):
    with pytest.raises(SystemExit) as caught:
        cli.main([str(sample_path)] + arguments)
    assert caught.value.code == 2


def test_cap_five_accepted(sample_path, deps, monkeypatch):
    monkeypatch.setattr(cli, "build_real_deps", lambda _: deps)
    assert cli.main([str(sample_path), "--spend-cap-usd", "5"]) == 0


@pytest.mark.parametrize("dry_run", [False, True])
def test_send_missing_emails_before_steps(sample_path, monkeypatch, capsys, dry_run):
    monkeypatch.setattr(cli, "build_real_deps", lambda _: pytest.fail("Must fail before step 1"))
    args = [str(sample_path), "--send"] + (["--dry-run"] if dry_run else [])
    assert cli.main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert all(name in captured.err for name in secrets.EMAIL_NAMES)
    assert "@" not in captured.err


def test_send_requires_credentials_before_steps(sample_path, monkeypatch, capsys):
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", "buyer@example.invalid")
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", "seller@example.invalid")
    monkeypatch.setattr(cli, "build_real_deps", lambda _: pytest.fail("Must fail before step 1"))
    assert cli.main([str(sample_path), "--send"]) == 2
    text = capsys.readouterr().err
    assert all(name in text for name in cli.DOTTEDSIGN_NAMES) and "@" not in text


def test_dry_run_does_not_require_signing_credentials(sample_path, deps, monkeypatch):
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", "buyer@example.invalid")
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", "seller@example.invalid")
    monkeypatch.setattr(cli, "build_real_deps", lambda _: deps)
    assert cli.main([str(sample_path), "--send", "--dry-run"]) == 0
    assert deps.signer.calls[0][1]["dry_run"] is True


@pytest.mark.parametrize("command,expected", [("status", 0), ("fetch", 43)])
def test_tracking_needs_only_dottedsign(monkeypatch, command, expected):
    from types import SimpleNamespace
    from quote_sign.dottedsign import TaskStatus
    for name in cli.DOTTEDSIGN_NAMES:
        monkeypatch.setenv(name, "fake-dottedsign-credential-0001")
    monkeypatch.setattr(cli, "build_signing_client", lambda: SimpleNamespace(
        get_task=lambda _: TaskStatus(91, "waiting", None, ())))
    assert cli.main([command, "91"]) == expected


@pytest.mark.parametrize("arguments", [["status", "0"], ["fetch", "bad"], ["status", "91", "--watch", "21"],
                                       ["status", "91", "--watch", "0"]])
def test_tracking_usage_errors(arguments):
    with pytest.raises(SystemExit) as caught:
        cli.main(arguments)
    assert caught.value.code == 2


def test_keyboard_interrupt_is_clean(sample_path, monkeypatch, capsys):
    def interrupt(_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, "build_real_deps", interrupt)
    assert cli.main([str(sample_path)]) == 130
    text = capsys.readouterr().err
    assert text == "已中斷。\n" and "Traceback" not in text


def test_signer_factory_is_lazy_and_fixed_log_location(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from quote_sign.sign import DottedSignSigner
    monkeypatch.setenv("QUOTE_SIGN_BUYER_EMAIL", "buyer@example.invalid")
    monkeypatch.setenv("QUOTE_SIGN_SELLER_EMAIL", "seller@example.invalid")
    monkeypatch.setattr(cli, "build_signing_client", lambda: pytest.fail("Must be lazy"))
    signer = cli.build_signer(SimpleNamespace(send=True, dry_run=True))
    assert isinstance(signer, DottedSignSigner)
    assert signer.task_log.path == paths.state_dir() / "sign-tasks.jsonl"
    assert not signer.task_log.path.parent.exists()


def test_cli_watch_and_completed_fetch(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    from quote_sign import track
    from quote_sign.dottedsign import TaskStatus
    for name in cli.DOTTEDSIGN_NAMES:
        monkeypatch.setenv(name, "fake-dottedsign-credential-0001")
    sleeps = []
    states = iter(["waiting", "completed", "completed"])
    monkeypatch.setattr(track.time, "sleep", sleeps.append)
    monkeypatch.setattr(cli, "build_signing_client", lambda: SimpleNamespace(
        get_task=lambda _: TaskStatus(91, next(states), 1796486399, ()),
        download_signed=lambda _: b"%PDF-fake"))
    assert cli.main(["status", "91", "--watch", "3"]) == 0
    assert sleeps == [30]
    capsys.readouterr()
    assert cli.main(["fetch", "91", "--out-dir", str(tmp_path / "out")]) == 0
    output = capsys.readouterr().out
    assert '"task_id": 91' in output and "@" not in output
    assert (tmp_path / "out" / "signed" / "task-91-signed.pdf").read_bytes() == b"%PDF-fake"
