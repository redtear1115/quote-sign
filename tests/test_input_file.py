from __future__ import annotations

from dataclasses import FrozenInstanceError
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from quote_sign import cli, input_file
from quote_sign.errors import ExportError, InputNotFound, InputRejected
from quote_sign.export_pdf import export_pdf
from quote_sign.input_file import InputDoc, load_markdown
from quote_sign.pipeline import run

ORIGINAL = "# 假報價單\n\n原始內容。\n".encode("utf-8") + b"\x00\xff"
FAKE_SECRET = b"FAKE_SECRET=not-a-real-value"


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("name", ["quote.md", "QUOTE.MD"])
def test_exact_bytes(tmp_path, monkeypatch, absolute, name):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / name
    path.write_bytes(ORIGINAL)
    doc = load_markdown(path if absolute else Path(".") / name)
    assert doc.name == name and doc.data == ORIGINAL
    assert doc.sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    with pytest.raises(FrozenInstanceError):
        doc.data = b"changed"


def test_character_device_mode_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "quote.md"
    path.write_bytes(FAKE_SECRET)
    info = path.stat()
    values = list(info)
    values[0] = stat.S_IFCHR | 0o600
    monkeypatch.setattr(input_file.os, "fstat", lambda fd: os.stat_result(values))
    with pytest.raises(InputRejected, match="不是一般檔案") as caught:
        load_markdown(path)
    assert FAKE_SECRET.decode() not in str(caught.value)


@pytest.mark.parametrize("fails", [False, True])
def test_export_uses_private_snapshot_and_cleans_up(tmp_path, monkeypatch, fails):
    # Keep every temporary artifact in this test's fake directory.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    path = tmp_path / "quote.md"
    path.write_bytes(ORIGINAL)
    doc = load_markdown(path)
    path.write_bytes(FAKE_SECRET)
    copies = []

    def runner(command, **kwargs):
        copy = Path(command[2])
        copies.append(copy)
        assert copy != path
        assert copy.name == path.name
        assert copy.read_bytes() == ORIGINAL
        assert list(copy.parent.iterdir()) == [copy]
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert command == ["marsdawn", "export", str(copy), "-o",
                           str(copy.with_suffix(".pdf")), "--force", "--json"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 120}
        if fails:
            raise subprocess.TimeoutExpired("marsdawn", 120)
        Path(command[4]).write_bytes(b"%PDF-fake")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "ok": True, "output": command[4], "pages": 1,
        }))

    if fails:
        with pytest.raises(ExportError):
            export_pdf(doc, tmp_path / "out", runner=runner)
    else:
        assert export_pdf(doc, tmp_path / "out", runner=runner).pages == 1
    assert len(copies) == 1
    assert not copies[0].parent.exists()
    assert path.read_bytes() == FAKE_SECRET


def test_pipeline_signs_snapshot_hash_after_original_changes(tmp_path, deps):
    path = tmp_path / "quote.md"
    path.write_bytes(ORIGINAL)
    doc = load_markdown(path)
    path.write_bytes(FAKE_SECRET)
    assert run(doc, deps, send=True) == 0
    assert deps.export.calls == [(doc, deps.out_dir)]
    (request,), _ = deps.signer.calls[0]
    assert request.source_sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    assert request.source_sha256 != hashlib.sha256(FAKE_SECRET).hexdigest()


@pytest.mark.parametrize("kind,category", [
    ("extension", "副檔名不是 .md"), ("parent", "路徑含 .."),
    ("symlink", "路徑含符號連結"), ("directory", "不是一般檔案"),
    ("hardlink", "有多個硬連結"), ("open_error", "無法安全開啟"),
])
def test_rejection_messages_and_repr_do_not_expose_content(tmp_path, monkeypatch, kind, category):
    path = tmp_path / "quote.md"
    path.write_bytes(FAKE_SECRET)
    assert FAKE_SECRET.decode() not in repr(load_markdown(path))
    if kind == "extension":
        path = path.rename(tmp_path / "quote.txt")
    elif kind == "parent":
        path = tmp_path / ".." / tmp_path.name / path.name
    elif kind == "symlink":
        link = tmp_path / "link.md"
        link.symlink_to(path)
        path = link
    elif kind == "directory":
        path = tmp_path / "directory.md"
        path.mkdir()
    elif kind == "hardlink":
        os.link(path, tmp_path / "other.md")
    elif kind == "open_error":
        def fail(*args, **kwargs):
            raise PermissionError(errno.EACCES, FAKE_SECRET.decode(), "/fake/resolved/target")
        monkeypatch.setattr(input_file.os, "open", fail)
    with pytest.raises(InputRejected) as caught:
        load_markdown(path)
    assert caught.value.category == category
    assert str(caught.value) == category
    assert FAKE_SECRET.decode() not in repr(caught.value)
    assert "/fake/resolved/target" not in repr(caught.value)


def test_pipeline_rejects_symlink_before_export(tmp_path, deps, capsys):
    target = tmp_path / "target.md"
    target.write_bytes(FAKE_SECRET)
    link = tmp_path / "alias.md"
    link.symlink_to(target)
    assert run(link, deps, send=True) == 3
    assert not deps.export.calls and not deps.reader.calls
    assert not deps.llm.calls and not deps.signer.calls
    captured = capsys.readouterr()
    assert captured.err == f"輸入檔不符規則（路徑含符號連結）：{link}\n"
    assert not captured.out
    assert str(target) not in captured.err
    assert FAKE_SECRET.decode() not in captured.err


def test_pipeline_missing_input_before_export(tmp_path, deps, capsys):
    path = tmp_path / "missing.md"
    assert run(path, deps) == 2
    assert not deps.export.calls
    assert capsys.readouterr().err == f"找不到檔案：{path}\n"


@pytest.mark.parametrize("component", ["missing", "file.md"])
def test_missing_or_non_directory_component(tmp_path, component):
    (tmp_path / "file.md").write_bytes(ORIGINAL)
    with pytest.raises(InputNotFound) as caught:
        load_markdown(tmp_path / component / "quote.md")
    assert str(caught.value) == ""


@pytest.mark.parametrize("name", ["", ".", "..", "../x.md", "a/b.md", "x\x00.md"])
def test_input_doc_name_must_be_bare_file_name(name):
    with pytest.raises(InputRejected):
        InputDoc(name, b"")


def test_nul_byte_rejected(tmp_path):
    with pytest.raises(InputRejected, match="無法安全開啟"):
        load_markdown(tmp_path / "invalid\x00.md")


def test_unsupported_descriptor_relative_open_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "quote.md"
    path.write_bytes(ORIGINAL)
    real_open = os.open
    real_close = os.close
    opened, closed = [], []

    def unsupported(path, flags, **kwargs):
        if "dir_fd" in kwargs:
            raise NotImplementedError("dir_fd unavailable")
        fd = real_open(path, flags, **kwargs)
        opened.append(fd)
        return fd

    def close(fd):
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(input_file.os, "open", unsupported)
    monkeypatch.setattr(input_file.os, "close", close)
    with pytest.raises(InputRejected, match="無法安全開啟"):
        load_markdown(path)
    assert opened and closed == opened


def test_cli_preserves_typed_path_and_rejects_before_model_or_secret_checks(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.txt").write_bytes(FAKE_SECRET)
    monkeypatch.setattr(cli, "build_real_deps", lambda _: pytest.fail("No dependencies allowed"))
    monkeypatch.setattr(cli.secrets, "require_signer_emails", lambda: pytest.fail("No secret checks allowed"))
    typed = "././bad.txt"
    assert cli.main([typed, "--send", "--extract-model", "same", "--review-model", "same"]) == 3
    captured = capsys.readouterr()
    assert captured.err == f"輸入檔不符規則（副檔名不是 .md）：{typed}\n"
    assert not captured.out
