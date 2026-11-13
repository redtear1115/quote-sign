from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from quote_sign.dottedsign import StageStatus, TaskStatus
from quote_sign.errors import SigningError
from quote_sign.sign import TaskLog
from quote_sign.track import fetch, status


def task(state):
    return TaskStatus(91, state, 1796486399, (StageStatus(2, "done"), StageStatus(1, "signed")))


@pytest.mark.parametrize("state,code", [("draft", 0), ("waiting", 0), ("completed", 0),
    ("declined", 42), ("expired", 42), ("voided", 42), ("deleted", 42), ("unknown", 42)])
def test_status_roles_and_codes(state, code, capsys):
    assert status(SimpleNamespace(get_task=lambda _: task(state)), 91) == code
    assert f"任務 91：狀態={state}；買方=signed；賣方=done" in capsys.readouterr().out


@pytest.mark.parametrize("states,count,delays", [(["waiting", "completed", "waiting"], 2, [30]),
                                               (["waiting"]*4, 3, [30, 30]),
                                               (["declined"]*4, 1, [])])
def test_watch_bounded_and_stops(states, count, delays):
    calls, sleeps = [], []
    def get(_):
        calls.append(True)
        return task(states[len(calls)-1])
    status(SimpleNamespace(get_task=get), 91, watch=3, sleep=sleeps.append)
    assert len(calls) == count and sleeps == delays


def test_fetch_before_complete(tmp_path, capsys):
    client = SimpleNamespace(get_task=lambda _: task("waiting"),
                             download_signed=lambda _: pytest.fail("Must not download"))
    assert fetch(client, 91, TaskLog(tmp_path / "state" / "sign-tasks.jsonl"), tmp_path / "out") == 43
    assert "尚未完成" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("known", [True, False])
def test_fetch_pdf_and_one_private_record(tmp_path, capsys, known):
    log = TaskLog(tmp_path / "state" / "sign-tasks.jsonl")
    if known:
        log.begin(run_id="fake-run-0001", quote_no="Q2026-031", source_sha256="a"*64,
                  pdf_sha256="b"*64, buyer_name="範例買方", seller_name="範例賣方")
        log.finish("fake-run-0001", "created", 91)
    pdf = b"%PDF-fake-signed"
    client = SimpleNamespace(get_task=lambda _: task("completed"), download_signed=lambda _: pdf)
    assert fetch(client, 91, log, tmp_path / "out") == 0
    directory = tmp_path / "out" / "signed"
    assert (directory / (("Q2026-031" if known else "task-91") + "-signed.pdf")).read_bytes() == pdf
    text = (directory / "signed-log.jsonl").read_text()
    assert len(text.splitlines()) == 1 and "@" not in text
    row = json.loads(text)
    assert row["signed_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert row["pdf_sha256"] == ("b"*64 if known else None)
    assert row["source_sha256"] == ("a"*64 if known else None)
    assert row["signers"][0] == {"role": "買方", "name": "範例買方" if known else None, "status": "signed"}
    assert row["completed_at"].endswith("+08:00")
    assert datetime.fromisoformat(row["completed_at"]).timestamp() == task("completed").last_modified_at
    assert json.loads(capsys.readouterr().out) == row


@pytest.mark.parametrize("target", ["task-91-signed.pdf", "signed-log.jsonl", "signed-log.lock", "signed"])
def test_fetch_symlinks_refused(tmp_path, target):
    directory = tmp_path / "out" / "signed"
    directory.mkdir(parents=True)
    dest = tmp_path / "dest"
    if target == "signed":
        directory.rmdir()
        dest.mkdir()
        directory.symlink_to(dest, target_is_directory=True)
    else:
        dest.write_text("unchanged")
        (directory / target).symlink_to(dest)
    client = SimpleNamespace(get_task=lambda _: task("completed"), download_signed=lambda _: b"%PDF-fake")
    with pytest.raises(SigningError):
        fetch(client, 91, TaskLog(tmp_path / "state" / "sign-tasks.jsonl"), tmp_path / "out")
    if dest.is_file():
        assert dest.read_text() == "unchanged"


def test_fetch_refuses_symlink_ancestor(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    out = tmp_path / "out"
    out.symlink_to(dest, target_is_directory=True)
    client = SimpleNamespace(get_task=lambda _: task("completed"), download_signed=lambda _: b"%PDF-fake")
    with pytest.raises(SigningError):
        fetch(client, 91, TaskLog(tmp_path / "state" / "sign-tasks.jsonl"), out)
    assert not list(dest.iterdir())
