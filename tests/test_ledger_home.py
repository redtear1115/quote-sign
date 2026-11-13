"""Account-based state and fail-closed CLI preflight, using temporary files only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import stat
from types import SimpleNamespace

import pytest

from conftest import REAL_LEDGER_HOME
from quote_sign import cli, paths
from quote_sign.errors import BudgetExceeded, LocalStateError
from quote_sign.llm import SpendLedger
from quote_sign.transport import UrllibTransport

CANARY = "CANARY-LEDGER-7f3a"
HOME_ERROR = "找不到系統帳號的家目錄，拒絕執行"
LEDGER_ERROR = "花費紀錄損毀或無法讀取，拒絕執行"


def entry(cost, **extra):
    return dict(ts="2026-11-13T00:00:00+00:00", model="deepseek-v4-flash",
                purpose="抽欄位", prompt_tokens=10, completion_tokens=5,
                cost_usd=cost, estimate=True, **extra)


def ledger_bytes(total="5", calls=None):
    return json.dumps({"total_usd": total, "calls": [entry(total)] if calls is None else calls,
                       "canary": CANARY}).encode("utf-8")


@pytest.fixture
def account(monkeypatch, tmp_path):
    home = tmp_path / "acct"
    monkeypatch.setattr(paths.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(home)))
    monkeypatch.setattr(paths, "ledger_home", REAL_LEDGER_HOME)
    return home


@pytest.fixture
def no_services(monkeypatch):
    for name, value in {
        "MIX_ROUTE_API_KEY": "fake-mixroute-key",
        "MIX_ROUTE_BASE_URL": "https://mix.invalid/v1",
        "COMPDF_PUBLIC_KEY": "fake-compdf-public",
        "COMPDF_SECRET_KEY": "fake-compdf-secret",
    }.items():
        monkeypatch.setenv(name, value)
    calls = []

    def unexpected(*args, **kwargs):
        calls.append(True)
        pytest.fail("Preflight must stop before PDF export or any service call")

    monkeypatch.setattr(cli, "export_pdf", unexpected)
    monkeypatch.setattr(UrllibTransport, "request", unexpected)
    return calls


def test_home_cannot_bypass_budget(account, monkeypatch, tmp_path):
    first, second = tmp_path / "h1", tmp_path / "h2"
    monkeypatch.setenv("HOME", str(first))
    assert paths.state_dir() == account / ".quote-sign"
    SpendLedger(paths.state_dir() / "spend.json", Decimal("5")).reserve(
        "deepseek-v4-flash", "抽欄位", Decimal("4.99"))
    monkeypatch.setenv("HOME", str(second))
    assert paths.state_dir() == account / ".quote-sign"
    with pytest.raises(BudgetExceeded):
        SpendLedger(paths.state_dir() / "spend.json", Decimal("5")).check(Decimal("0.02"))
    assert not first.exists() and not second.exists()


def test_cli_cap_preflight_ignores_home(account, no_services, monkeypatch, tmp_path,
                                      sample_path, capsys):
    ledger = paths.state_dir() / "spend.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(ledger_bytes())
    before = ledger.read_bytes()
    other_home = tmp_path / "other-home"
    monkeypatch.setenv("HOME", str(other_home))
    assert cli.main([str(sample_path)]) == 31
    assert "累計花費已達上限" in capsys.readouterr().err
    assert ledger.read_bytes() == before
    assert not no_services and not other_home.exists()


@pytest.mark.parametrize("error", [KeyError, OSError, RuntimeError])
def test_account_lookup_failure_stops_cli(account, no_services, monkeypatch, tmp_path,
                                         sample_path, capsys, error):
    home = tmp_path / "fallback-home"
    monkeypatch.setenv("HOME", str(home))

    def missing(uid):
        raise error(CANARY)

    monkeypatch.setattr(paths.pwd, "getpwuid", missing)
    with pytest.raises(LocalStateError, match=HOME_ERROR) as caught:
        paths.ledger_home()
    assert caught.value.__suppress_context__
    assert cli.main([str(sample_path)]) == 32
    output = capsys.readouterr()
    assert output.err == HOME_ERROR + "\n" and output.out == ""
    assert not no_services and not home.exists()


@pytest.mark.parametrize("home", ["", "relative/home", None, 42, b"/absolute"])
def test_invalid_account_home(account, monkeypatch, home):
    monkeypatch.setattr(paths.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=home))
    with pytest.raises(LocalStateError, match=HOME_ERROR) as caught:
        paths.ledger_home()
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("command", ["status", "fetch"])
def test_tracking_account_failure(account, no_services, monkeypatch, capsys, command):
    for name in cli.DOTTEDSIGN_NAMES:
        monkeypatch.setenv(name, "fake-dottedsign-key")
    monkeypatch.setattr(paths.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=""))
    monkeypatch.setattr(cli, "build_signing_client", lambda: pytest.fail("Must check account first"))
    assert cli.main([command, "91"]) == 32
    assert capsys.readouterr().err == HOME_ERROR + "\n"
    assert not no_services


BAD_BYTES = {
    "invalid-json": (CANARY + " not JSON").encode(),
    "truncated-json": ('{"canary":"' + CANARY + '","total_usd":').encode(),
    "empty": b"",
    "non-utf8": CANARY.encode() + b"\xff",
    "negative-total": ledger_bytes("-1"),
    "text-total": ledger_bytes(CANARY),
    "list": json.dumps([CANARY]).encode(),
    "inconsistent-total": ledger_bytes("0", [entry("5")]),
}


def snapshot(path):
    info = os.lstat(path)
    state = (info.st_mode, info.st_mtime_ns, info.st_size, info.st_ino)
    if stat.S_ISREG(info.st_mode):
        return state, path.read_bytes()
    if stat.S_ISLNK(info.st_mode):
        return state, os.readlink(path)
    if stat.S_ISDIR(info.st_mode):
        return state, tuple(sorted(p.name for p in path.iterdir()))
    return state, None


@pytest.mark.parametrize("kind", list(BAD_BYTES) + [
    "directory", "symlink", "dangling-symlink", "fifo", "unreadable"])
def test_unusable_ledger_stops_before_export(account, no_services, sample_path,
                                           tmp_path, capsys, kind):
    path = paths.state_dir() / "spend.json"
    path.parent.mkdir(parents=True)
    target = tmp_path / "valid-ledger.json"
    if kind in BAD_BYTES:
        path.write_bytes(BAD_BYTES[kind])
    elif kind == "directory":
        path.mkdir()
        (path / CANARY).write_text(CANARY)
    elif kind in ("symlink", "dangling-symlink"):
        if kind == "symlink":
            target.write_bytes(ledger_bytes("1"))
        path.symlink_to(target)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(path)
    elif kind == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root can read mode 000")
        path.write_bytes(ledger_bytes("1"))
        path.chmod(0)

    # Read only while permissions allow it, then restore mode 000 for the CLI.
    if kind == "unreadable":
        path.chmod(0o600)
        content_before = path.read_bytes()
        path.chmod(0)
        before = os.lstat(path)
    else:
        before = snapshot(path)
    target_before = snapshot(target) if target.exists() else None

    previous_alarm = None
    if kind == "fifo":
        def timeout(signum, frame):
            raise TimeoutError("Ledger FIFO blocked")
        previous_alarm = signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        assert cli.main([str(sample_path)]) == 32
    finally:
        if kind == "fifo":
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_alarm)
        if kind == "unreadable":
            after = os.lstat(path)
            path.chmod(0o600)

    output = capsys.readouterr()
    assert output.err == LEDGER_ERROR + "\n" and output.out == ""
    assert CANARY not in output.err + output.out
    assert not no_services
    if kind == "unreadable":
        assert (before.st_mode, before.st_mtime_ns, before.st_size, before.st_ino) == (
            after.st_mode, after.st_mtime_ns, after.st_size, after.st_ino)
        assert path.read_bytes() == content_before
    else:
        assert snapshot(path) == before
    assert (snapshot(target) if target.exists() else None) == target_before


def test_first_use_preflight_and_private_creation(account, no_services, tmp_path):
    args = SimpleNamespace(spend_cap_usd=Decimal("5"), send=False,
                           extract_model="deepseek-v4-flash", review_model="gpt-6-sol",
                           today=date(2026, 11, 13), out_dir=tmp_path / "out")
    deps = cli.build_real_deps(args)
    ledger = deps.llm.ledger
    assert ledger.path == account / ".quote-sign" / "spend.json"
    assert not ledger.path.exists()
    ledger.reserve("deepseek-v4-flash", "抽欄位", Decimal("0.01"))
    assert stat.S_IMODE(ledger.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert ledger.total() == Decimal("0.01") and not no_services


def test_valid_ledger_with_legacy_and_current_calls(account):
    path = paths.state_dir() / "spend.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(ledger_bytes("1.35", [entry("0.10"), entry("0.20"),
        entry("1.00", id="pending-reservation", pending=True),
        entry("0.05", id="settled-reservation", pending=False)]))
    ledger = SpendLedger(path, Decimal("5"))
    assert ledger.total() == Decimal("1.35")
    ledger.check()


@pytest.mark.parametrize("kind", ["regular", "directory", "fstat-error"])
def test_ledger_fd_flags_type_check_and_cleanup(tmp_path, monkeypatch, kind):
    path = tmp_path / "spend.json"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(ledger_bytes("1"))
    original_open, original_fstat = os.open, os.fstat
    opened, checked = [], []

    def open_ledger(target, flags, *args, **kwargs):
        fd = original_open(target, flags, *args, **kwargs)
        if target == path:
            opened.append((fd, flags))
        return fd

    def fstat_ledger(fd):
        if opened and fd == opened[-1][0]:
            checked.append(fd)
            if kind == "fstat-error":
                raise OSError(CANARY)
        return original_fstat(fd)

    monkeypatch.setattr(os, "open", open_ledger)
    monkeypatch.setattr(os, "fstat", fstat_ledger)
    ledger = SpendLedger(path, Decimal("5"))
    if kind == "regular":
        assert ledger.total() == Decimal("1")
    else:
        with pytest.raises(LocalStateError, match=LEDGER_ERROR):
            ledger.total()
    assert len(opened) == 1
    fd, flags = opened[0]
    assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    assert checked == [fd]
    with pytest.raises(OSError):
        original_fstat(fd)
