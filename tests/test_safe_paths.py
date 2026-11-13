"""System aliases are the only symlinks admitted by descriptor traversal."""
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace

import pytest

from quote_sign import cli, safe_paths
from quote_sign.errors import InputNotFound, InputRejected
from quote_sign.export_pdf import export_pdf
from quote_sign.input_file import load_markdown
from quote_sign.safe_paths import check_out_dir, expand_system_alias, open_dir


@pytest.fixture
def scratch():
    directory = tempfile.mkdtemp(dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory)


@pytest.mark.parametrize("path,target,expected", [
    ("/tmp/a.md", "/private/tmp", "/private/tmp/a.md"),
    ("/var/folders/x/y", "/private/var", "/private/var/folders/x/y"),
    ("/tmp", "/private/tmp", "/private/tmp"),
    ("/etc/q.md", "/private/etc", "/private/etc/q.md"),
])
def test_root_owned_system_alias(path, target, expected):
    info = SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0)
    assert expand_system_alias(path, lambda _: info, lambda _: target) == expected


@pytest.mark.parametrize("uid,mode,target", [
    (501, stat.S_IFLNK, "/private/tmp"),
    (0, stat.S_IFDIR, "/private/tmp"),
    (0, stat.S_IFLNK, "/Users/u/tmp"),
    (0, stat.S_IFLNK, "/private"),
    (0, stat.S_IFLNK, "/private/"),
    (0, stat.S_IFLNK, "/private/a/b"),
    (0, stat.S_IFLNK, "/privatex/tmp"),
])
def test_alias_conditions_are_required(uid, mode, target):
    info = SimpleNamespace(st_mode=mode | 0o755, st_uid=uid)
    assert expand_system_alias("/tmp/a.md", lambda _: info, lambda _: target) == "/tmp/a.md"


@pytest.mark.parametrize("operation", ["lstat", "realpath"])
def test_alias_exceptions_leave_path_unchanged(operation):
    def fail(_):
        raise OSError("fake failure")
    info = SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0)
    lstat = fail if operation == "lstat" else lambda _: info
    realpath = fail if operation == "realpath" else lambda _: "/private/tmp"
    assert expand_system_alias("/tmp/a.md", lstat, realpath) == "/tmp/a.md"


@pytest.mark.parametrize("path", ["tmp/a.md", "/", ""])
def test_paths_without_first_absolute_component_are_untouched(path):
    def fail(_):
        pytest.fail("No filesystem inspection expected")
    assert expand_system_alias(path, fail, fail) == path


def test_only_first_component_is_inspected():
    calls = []
    def lstat(path):
        calls.append(("lstat", path))
        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o755, st_uid=0)
    def realpath(path):
        calls.append(("realpath", path))
        return "/private/tmp"
    assert expand_system_alias("/tmp/linked/deeper/q.md", lstat, realpath) == "/private/tmp/linked/deeper/q.md"
    assert calls == [("lstat", "/tmp"), ("realpath", "/tmp")]


@pytest.mark.parametrize("relative", [False, True])
def test_directory_preflight_and_creation(scratch, monkeypatch, relative):
    monkeypatch.chdir(scratch)
    path = Path("new/deep/out") if relative else scratch / "new/deep/out"
    check_out_dir(path)
    assert not path.exists()
    with pytest.raises(InputNotFound):
        open_dir(path)
    fd = open_dir(path, create=True, mode=0o700)
    try:
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind,category", [
    ("middle", "路徑含符號連結"), ("last", "路徑含符號連結"),
    ("parent", "路徑含 .."), ("file", "不是目錄"),
])
@pytest.mark.parametrize("command", ["quote", "fetch"])
def test_cli_rejects_output_before_settings_and_services(scratch, monkeypatch, capsys, kind, category, command):
    monkeypatch.chdir(scratch)
    (scratch / "q.md").write_bytes(b"# Fake quote\n")
    (scratch / "target").mkdir()
    if kind in ("middle", "last"):
        (scratch / "link").symlink_to(scratch / "target", target_is_directory=True)
        typed = "././link/out" if kind == "middle" else "././link"
    elif kind == "parent":
        typed = "./target/../out"
    else:
        (scratch / "file").write_bytes(b"fake")
        typed = "././file/out"
    with pytest.raises(InputRejected, match=category):
        check_out_dir(typed)
    with pytest.raises(InputRejected, match=category):
        open_dir(typed, create=True)
    def forbidden(*args, **kwargs):
        pytest.fail("Output rejection must precede dependencies, settings and services")
    for name in ("build_real_deps", "export_pdf", "build_signing_client", "TaskLog"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(cli.secrets, "require", forbidden)
    monkeypatch.setattr(cli.secrets, "require_signer_emails", forbidden)
    args = ["q.md", "--send", "--extract-model", "same", "--review-model", "same"]
    if command == "fetch":
        args = ["fetch", "91"]
    assert cli.main(args + ["--out-dir", typed]) == 3
    captured = capsys.readouterr()
    assert captured.err == f"輸出目錄不符規則（{category}）：{typed}\n"
    assert not captured.out
    assert not (scratch / "target/out").exists()


@pytest.mark.parametrize("kind,category", [
    ("middle", "路徑含符號連結"), ("last", "路徑含符號連結"),
    ("hardlink", "有多個硬連結"), ("parent", "路徑含 .."),
])
def test_input_still_rejects_unsafe_paths_under_tmp(scratch, kind, category):
    directory = scratch / "docs"
    directory.mkdir()
    path = directory / "q.md"
    path.write_bytes(b"# Fake quote\n")
    if kind == "middle":
        (scratch / "link").symlink_to(directory, target_is_directory=True)
        path = scratch / "link/q.md"
    elif kind == "last":
        (directory / "link.md").symlink_to(path)
        path = directory / "link.md"
    elif kind == "hardlink":
        os.link(path, directory / "second.md")
    else:
        path = directory / ".." / "docs/q.md"
    with pytest.raises(InputRejected, match=category):
        load_markdown(path)


def test_mkdir_race_still_rejects_planted_symlink(scratch, monkeypatch):
    target = scratch / "target"
    target.mkdir()
    original_mkdir = os.mkdir
    def mkdir(name, mode=0o777, *, dir_fd=None):
        if name == "out":
            os.symlink(target, name, dir_fd=dir_fd)
            raise FileExistsError(errno.EEXIST, "fake race")
        return original_mkdir(name, mode, dir_fd=dir_fd)
    monkeypatch.setattr(safe_paths.os, "mkdir", mkdir)
    with pytest.raises(InputRejected, match="路徑含符號連結"):
        open_dir(scratch / "out", create=True)
    assert not list(target.iterdir())


@pytest.mark.parametrize("action", ["check", "open", "reject"])
def test_walk_closes_intermediate_descriptors(scratch, monkeypatch, action):
    (scratch / "one/two").mkdir(parents=True)
    (scratch / "one/two/file").write_bytes(b"fake")
    original_open, original_close = os.open, os.close
    active = set()
    def opening(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        active.add(fd)
        return fd
    def closing(fd):
        active.remove(fd)
        original_close(fd)
    monkeypatch.setattr(safe_paths.os, "open", opening)
    monkeypatch.setattr(safe_paths.os, "close", closing)
    if action == "check":
        check_out_dir(scratch / "one/two/missing")
    elif action == "open":
        fd = open_dir(scratch / "one/two")
        assert active == {fd}
        os.close(fd)
    else:
        with pytest.raises(InputRejected, match="不是目錄"):
            open_dir(scratch / "one/two/file")
    assert not active


macos_aliases = pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")


@macos_aliases
@pytest.mark.parametrize("under_tmp", [False, True])
def test_macos_input_system_alias(under_tmp):
    directory = tempfile.mkdtemp(dir="/tmp") if under_tmp else tempfile.mkdtemp()
    try:
        path = Path(directory) / "q.md"
        path.write_bytes(b"# Fake quote\n")
        # macOS's default temp directory may be returned in canonical form.
        typed = str(path)
        if not under_tmp and typed.startswith("/private/var/"):
            typed = typed[len("/private"):]
        assert load_markdown(Path(typed)).data == b"# Fake quote\n"
    finally:
        shutil.rmtree(directory)


@macos_aliases
def test_macos_output_and_export_system_alias(scratch):
    out = scratch / "out"
    check_out_dir(out)
    assert not out.exists()
    def runner(command, **kwargs):
        Path(command[4]).write_bytes(b"%PDF-fake")
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"pages":1}')
    from quote_sign.input_file import InputDoc
    result = export_pdf(InputDoc("q.md", b"# Fake quote\n"), out, runner)
    assert result.path == out / "q.pdf"
    assert result.path.read_bytes() == b"%PDF-fake"


@macos_aliases
def test_macos_cli_reaches_dependency_builder(scratch, monkeypatch):
    path = scratch / "q.md"
    path.write_bytes(b"# Fake quote\n")
    calls = []
    marker = object()
    def build(args):
        calls.append(args)
        return marker
    monkeypatch.setattr(cli, "build_real_deps", build)
    monkeypatch.setattr(cli, "run", lambda doc, deps, **kwargs: 17 if deps is marker else 18)
    assert cli.main([str(path), "--out-dir", str(scratch / "out")]) == 17
    assert len(calls) == 1
