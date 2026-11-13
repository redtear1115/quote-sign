from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from quote_sign.errors import SigningError, SigningQuotaExceeded
from quote_sign.sign import (DottedSignSigner, SignRequest, TaskLog, build_task_body,
                             describe_task_body, field_coords)
from quote_sign.transport import TransportError

BUYER = "buyer@example.invalid"
SELLER = "seller@example.invalid"
BUYER_ANCHOR = (1, (87, 1165, 593, 1196))
SELLER_ANCHOR = (1, (86, 1226, 594, 1258))


@pytest.fixture
def request_pdf(tmp_path):
    path = tmp_path / "quote.pdf"
    path.write_bytes(b"%PDF-fake-only")
    return SignRequest(path, "a" * 64, "Q2026-031", "範例買方", "範例賣方", date(2026, 12, 5),
                       BUYER_ANCHOR, SELLER_ANCHOR)


@pytest.mark.parametrize("anchor", [BUYER_ANCHOR, SELLER_ANCHOR])
def test_coords_from_sample(anchor):
    page, (x0, y0, x1, y1) = anchor
    char_width = (x1 - x0) * .5 / 21
    expected_signature = [round(v, 1) for v in (x0*.5 + char_width*4, 842-y1*.5,
                                                x0*.5 + char_width*13, 842-y0*.5+10)]
    expected_date = [round(v, 1) for v in (x0*.5 + char_width*16, 842-y1*.5, x1*.5, 842-y0*.5)]
    assert field_coords(anchor) == (page-1, expected_signature, expected_date)


@pytest.mark.parametrize("anchor", [None, (), (0, (1, 1, 2, 2)), (1, (-1, 1, 2, 2)),
                                     (1, (1, 1, 1191, 1685)), (1, (1, 30, 1, 40)),
                                     (1, (1, float("nan"), 3, 4))])
def test_bad_anchors_refused(anchor):
    with pytest.raises(SigningError, match="找不到簽名位置"):
        field_coords(anchor)


def test_task_body_shape(request_pdf):
    body = build_task_body(request_pdf, BUYER, SELLER, b"%PDF-fake", "run000000001")
    assert body["file_name"] == "quote-Q2026-031.pdf"
    assert body["has_order"] is True and body["start"] is True
    assert [s["email"] for s in body["stages"]] == [BUYER, SELLER]
    assert [s["name"] for s in body["stages"]] == [request_pdf.buyer_name, request_pdf.seller_name]
    for stage in body["stages"]:
        assert stage["stage_type"] == "sign"
        sig, dt = stage["field_settings"]
        assert sig["page"] == dt["page"] == 0
        assert sig["options"] == {"force": True}
        assert dt["field_type"] == "datefield" and dt["options"]["date_format"] == "yyyy-mm-dd"
        assert "search_key" not in json.dumps(stage)
    assert body["metadata"] == {"quote_no": "Q2026-031", "source_sha256": "a" * 40,
                                "quote_sign_run": "run000000001"}
    assert "@" not in json.dumps(body["metadata"])
    assert body["task_setting"]["deadline"] == int(datetime(2026, 12, 5, 23, 59, 59,
                                                          tzinfo=timezone(timedelta(hours=8))).timestamp())
    assert "deadline" not in build_task_body(replace(request_pdf, valid_until=None), BUYER, SELLER,
                                             b"%PDF-fake", "run")["task_setting"]
    lines = "\n".join(describe_task_body(body))
    assert "base64_file" in lines and "bytes，省略" in lines
    assert BUYER not in lines and SELLER not in lines and body["base64_file"] not in lines


@pytest.mark.parametrize("quote_no", ["../bad", ".hidden", "bad#", "", "a"*41])
def test_invalid_quote_number(request_pdf, quote_no):
    with pytest.raises(SigningError):
        build_task_body(replace(request_pdf, quote_no=quote_no), BUYER, SELLER, b"pdf", "run")


@pytest.fixture
def signing(tmp_path):
    calls = []
    client = SimpleNamespace(create_task=lambda body: calls.append(body) or 91)
    factory_calls = []
    def factory():
        factory_calls.append(True)
        return client
    log = TaskLog(tmp_path / "state" / "sign-tasks.jsonl")
    return DottedSignSigner(factory, BUYER, SELLER, log), log, calls, factory_calls, client


def events(log):
    return [json.loads(s) for s in log.path.read_text().splitlines()]


def test_dry_run_is_entirely_local(request_pdf, signing, capsys):
    signer, log, calls, factory_calls, _ = signing
    outcome = signer.send(request_pdf, dry_run=True)
    assert outcome.dry_run and outcome.task_id is None
    assert not calls and not factory_calls and not log.path.parent.exists()
    text = capsys.readouterr().out
    assert BUYER not in text and SELLER not in text
    assert "b***@e***.invalid" in text and "沒有送出" in text
    assert "JVBERi" not in text and "省略" in text
    assert "@" not in repr(signer) and "@" not in repr(request_pdf)


def test_real_send_reuse_and_resend(request_pdf, signing, capsys):
    signer, log, calls, _, _ = signing
    outcome = signer.send(request_pdf, dry_run=False)
    assert outcome.task_id == 91 and not outcome.reused
    rows = events(log)
    assert [r["state"] for r in rows] == ["sending", "created"]
    assert rows[0]["pdf_sha256"] == hashlib.sha256(request_pdf.pdf_path.read_bytes()).hexdigest()
    assert "@" not in log.path.read_text()
    assert stat.S_IMODE(log.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.lock_path.stat().st_mode) == 0o600
    assert signer.send(request_pdf, dry_run=False).reused
    assert len(calls) == 1
    assert signer.send(request_pdf, dry_run=False, resend=True).task_id == 91
    assert len(calls) == 2 and len(events(log)) == 4
    output = capsys.readouterr().out
    assert "--resend" in output and "不代簽" in output
    assert BUYER not in output and SELLER not in output


def test_local_limit_before_factory(request_pdf, signing):
    signer, log, calls, factory_calls, _ = signing
    signer.local_limit = 1
    signer.send(request_pdf, dry_run=False)
    with pytest.raises(SigningQuotaExceeded):
        signer.send(request_pdf, dry_run=False, resend=True)
    assert len(calls) == len(factory_calls) == 1
    assert log.count_last_24h(datetime.now(timezone.utc)) == 1


@pytest.mark.parametrize("error,state", [(SigningQuotaExceeded("HTTP 403"), "failed"),
                                         (TransportError("sensitive"), "unknown"),
                                         (RuntimeError("sensitive"), "unknown"),
                                         (KeyboardInterrupt(), "unknown")])
def test_failures_recorded(request_pdf, signing, error, state):
    signer, log, _, _, client = signing
    def fail(body):
        raise error
    client.create_task = fail
    expected = type(error)
    with pytest.raises(expected):
        signer.send(request_pdf, dry_run=False)
    assert [r["state"] for r in events(log)] == ["sending", state]
    assert log.count_last_24h(datetime.now(timezone.utc)) == 1
    if state == "unknown":
        assert signer.send(request_pdf, dry_run=False).reused


def test_http_400_is_failed(request_pdf, signing):
    signer, log, _, _, client = signing
    error = SigningError("HTTP 400")
    error.http_status = 400
    def fail(body):
        raise error
    client.create_task = fail
    with pytest.raises(SigningError):
        signer.send(request_pdf, dry_run=False)
    assert events(log)[-1]["state"] == "failed"


@pytest.mark.parametrize("target", ["sign-tasks.jsonl", "sign-tasks.lock"])
def test_symlink_refused(request_pdf, tmp_path, target):
    destination = tmp_path / "destination"
    destination.write_text("untouched")
    (tmp_path / target).symlink_to(destination)
    signer = DottedSignSigner(lambda: pytest.fail("No API call"), BUYER, SELLER,
                              TaskLog(tmp_path / "sign-tasks.jsonl"))
    with pytest.raises(SigningError):
        signer.send(request_pdf, dry_run=False)
    assert destination.read_text() == "untouched"


def test_count_first_event_and_latest_active(tmp_path):
    log = TaskLog(tmp_path / "state" / "sign-tasks.jsonl")
    now = datetime.now(timezone.utc)
    for run_id, offset, state in (("old", 25, "created"), ("new", 0, "unknown"), ("failed", 0, "failed")):
        log.begin(run_id=run_id, quote_no="Q2026-031", source_sha256="a"*64, pdf_sha256="b"*64,
                  buyer_name="買方", seller_name="賣方", now=now-timedelta(hours=offset))
        log.finish(run_id, state, 91 if state == "created" else None)
    assert log.count_last_24h(now) == 2
    assert log.find_active("Q2026-031", "a"*64)["run_id"] == "new"
    assert log.find_task(91)["run_id"] == "old"
    assert log.find_task(999) is None


@pytest.mark.parametrize("content", [b"not PDF", b"%PDF-" + b"a" * (20*1024*1024)])
def test_invalid_pdf_before_log(request_pdf, signing, content):
    request_pdf.pdf_path.write_bytes(content)
    signer, log, _, factory_calls, _ = signing
    with pytest.raises(SigningError):
        signer.send(request_pdf, dry_run=False)
    assert not factory_calls and not log.path.exists()


def test_unknown_factory_exception_keeps_type_without_sensitive_context(request_pdf, tmp_path):
    log = TaskLog(tmp_path / "state" / "sign-tasks.jsonl")
    def fail():
        raise RuntimeError(BUYER + " https://download.example.invalid?X-Amz-Signature=fake")
    signer = DottedSignSigner(fail, BUYER, SELLER, log)
    with pytest.raises(RuntimeError) as caught:
        signer.send(request_pdf, dry_run=False)
    assert BUYER not in str(caught.value) and "X-Amz" not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert events(log)[-1]["state"] == "unknown"
