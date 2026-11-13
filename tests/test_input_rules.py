"""Input file rules, exercised through the CLI only (no new internal names).

Every file here is fake and lives under pytest's tmp_path. Links only point at
fake files created by the test itself.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from quote_sign import cli

REJECTED = 3
FAKE_SECRET = "FAKE_SECRET=not-a-real-value"
GOOD_MD = "# 報價單 Q2026-999（範例資料）\n\n假內容。\n"


@pytest.fixture
def builder(monkeypatch, deps):
    """Replace real client construction; record whether the pipeline got that far."""
    calls = []

    def build(args):
        calls.append(args)
        return deps

    monkeypatch.setattr(cli, "build_real_deps", build)
    return calls


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake checkout with a fake secret file outside it; cwd is the checkout."""
    root = tmp_path / "repo"
    (root / "sample").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text(FAKE_SECRET + "\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _assert_rejected(code, builder, deps, capsys, *hidden):
    out, err = capsys.readouterr()
    assert code == REJECTED
    assert builder == []
    assert not deps.export.calls and not deps.reader.calls
    assert not deps.llm.calls and not deps.signer.calls
    assert "輸入檔不符規則" in err
    for text in (FAKE_SECRET,) + hidden:
        assert text not in out and text not in err
    return err


def test_dev_a_repro_sample_replaced_by_link_to_fake_env(repo, builder, deps, capsys):
    link = repo / "sample" / "quote-Q2026-031.md"
    os.symlink(repo.parent / "outside" / ".env", link)
    code = cli.main(["sample/quote-Q2026-031.md", "--send", "--dry-run"])
    _assert_rejected(code, builder, deps, capsys, str(repo.parent / "outside"))


def test_link_to_md_inside_same_directory_is_still_rejected(repo, builder, deps, capsys):
    (repo / "sample" / "real.md").write_text(GOOD_MD, encoding="utf-8")
    os.symlink("real.md", repo / "sample" / "alias.md")
    _assert_rejected(cli.main(["sample/alias.md"]), builder, deps, capsys)


def test_dangling_link_is_rejected_not_missing(repo, builder, deps, capsys):
    os.symlink(repo.parent / "outside" / "gone.md", repo / "sample" / "dangling.md")
    _assert_rejected(cli.main(["sample/dangling.md"]), builder, deps, capsys)


def test_directory_component_link_relative(repo, builder, deps, capsys):
    target = repo.parent / "outside" / "docs"
    target.mkdir()
    (target / "quote.md").write_text(GOOD_MD, encoding="utf-8")
    os.symlink(target, repo / "linked")
    _assert_rejected(cli.main(["linked/quote.md"]), builder, deps, capsys, str(target))


def test_directory_component_link_absolute(repo, builder, deps, capsys):
    target = repo.parent / "outside" / "docs"
    target.mkdir()
    (target / "quote.md").write_text(GOOD_MD, encoding="utf-8")
    os.symlink(target, repo / "linked")
    _assert_rejected(cli.main([str(repo / "linked" / "quote.md")]), builder, deps, capsys)


def test_parent_reference_is_rejected(repo, builder, deps, capsys):
    (repo.parent / "outside" / "quote.md").write_text(GOOD_MD, encoding="utf-8")
    _assert_rejected(cli.main(["sample/../../outside/quote.md"]), builder, deps, capsys)


@pytest.mark.parametrize("name", ["quote.txt", "quote", "quote.md.txt", "quote.markdown",
                                  "quote.Md", ".env", ".md", "quote.md "])
def test_wrong_extension_is_rejected(repo, builder, deps, capsys, name):
    (repo / "sample" / name).write_text(FAKE_SECRET + "\n", encoding="utf-8")
    _assert_rejected(cli.main(["sample/" + name]), builder, deps, capsys)


def test_directory_named_md_is_rejected(repo, builder, deps, capsys):
    (repo / "sample" / "folder.md").mkdir()
    _assert_rejected(cli.main(["sample/folder.md"]), builder, deps, capsys)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFO support")
def test_fifo_named_md_is_rejected_without_blocking(repo, builder, deps, capsys):
    os.mkfifo(repo / "sample" / "pipe.md")
    _assert_rejected(cli.main(["sample/pipe.md"]), builder, deps, capsys)


def test_hard_link_is_rejected(repo, builder, deps, capsys):
    secret_copy = repo.parent / "outside" / "notes.md"
    secret_copy.write_text(FAKE_SECRET + "\n", encoding="utf-8")
    os.link(secret_copy, repo / "sample" / "quote.md")
    _assert_rejected(cli.main(["sample/quote.md"]), builder, deps, capsys)


def test_missing_file_keeps_exit_code_2(repo, builder, deps, capsys):
    assert cli.main(["sample/missing.md"]) == 2
    assert "找不到檔案" in capsys.readouterr().err
    assert builder == [] and not deps.export.calls


@pytest.mark.parametrize("name", ["quote.md", "QUOTE.MD"])
def test_regular_md_is_accepted(repo, builder, deps, capsys, name):
    (repo / "sample" / name).write_text(GOOD_MD, encoding="utf-8")
    code = cli.main(["sample/" + name])
    assert len(builder) == 1
    assert deps.export.calls
    assert code != REJECTED


def test_absolute_regular_md_is_accepted(repo, builder, deps):
    path = repo / "sample" / "quote.md"
    path.write_text(GOOD_MD, encoding="utf-8")
    cli.main([str(path)])
    assert len(builder) == 1 and deps.export.calls


def test_tracking_subcommands_do_not_take_an_input_file(monkeypatch, capsys):
    # status/fetch only take a task id; they must not be affected by the input rules.
    assert cli.main(["status", "12"]) == 2
    assert "輸入檔不符規則" not in capsys.readouterr().err
