import json
from urllib.parse import urlsplit

import pytest

from quote_sign.dottedsign import BASE_URL, DottedSignClient, StageStatus
from quote_sign.errors import SigningError, SigningQuotaExceeded
from quote_sign.transport import HttpResponse, TransportError

CLIENT_ID = "fake-dottedsign-client-id-0001"
CLIENT_SECRET = "fake-dottedsign-client-secret-0002"
TOKEN = "fake-dottedsign-access-token-0003"
EMAIL = "buyer@example.invalid"
LINK = "https://download.example.invalid/task.pdf?X-Amz-Signature=fake-presigned-0004"


def reply(data, status=200):
    return HttpResponse(status, json.dumps(data).encode())


def token(value=TOKEN, expires=3600):
    return reply({"access_token": value, "token_type": "Bearer", "expires_in": expires,
                  "scope": "sign_tasks", "created_at": 1})


def task(status="waiting", **extra):
    return reply({"data": {"task_id": 91, "status": status, "last_modified_at": 1796486399,
                           "details": [{"sequence": 0, "status": "sent", "name": "Owner", "email": EMAIL},
                                       {"sequence": 2, "status": "initial", "name": "Private seller", "email": EMAIL},
                                       {"sequence": 1, "status": "processing", "name": "Private buyer", "email": EMAIL}],
                           "current_stage_infos": [{"name": "Private buyer", "email": EMAIL}], **extra}})


def client(fake_transport, *responses, **kwargs):
    transport = fake_transport(*responses)
    return DottedSignClient(CLIENT_ID, CLIENT_SECRET, transport, **kwargs), transport


def assert_private(exc):
    while exc is not None:
        assert all(value not in str(exc) for value in (CLIENT_ID, CLIENT_SECRET, TOKEN, EMAIL, LINK))
        assert exc.__cause__ is None
        if exc.__suppress_context__:
            break
        exc = exc.__context__


def test_create_token_request_and_allowed_urls(fake_transport):
    c, transport = client(fake_transport, token(), reply({"data": {"task_id": 91}}), task(),
                          reply({"data": {"task": LINK}}), HttpResponse(200, b"%PDF-fake"))
    assert c.create_task({"file_name": "quote-Q2026-031.pdf"}) == 91
    assert c.get_task(91).status == "waiting"
    assert c.download_signed(91) == b"%PDF-fake"
    method, url, headers, body, timeout = transport.calls[0]
    assert (method, url, timeout) == ("POST", BASE_URL + "/v1/oauth/token", 60)
    assert headers == {"Content-Type": "application/json"}
    assert json.loads(body) == {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    allowed = {"/v1/oauth/token", "/v1/sign_tasks", "/v1/sign_tasks/download_link"}
    for method, url, headers, body, _ in transport.calls:
        assert url == LINK or (url.startswith(BASE_URL + "/") and urlsplit(url).path in allowed)
        if url != BASE_URL + "/v1/oauth/token":
            assert body is None or CLIENT_SECRET.encode() not in body
            assert body is None or CLIENT_ID.encode() not in body
        if url == LINK:
            assert headers == {}
        elif url != BASE_URL + "/v1/oauth/token":
            assert headers["Authorization"] == "Bearer " + TOKEN
    assert "file_types%5B%5D=task" in transport.calls[-2][1]
    assert repr(c) == "DottedSignClient(sandbox)"


def test_token_cache_refresh_and_401(fake_transport):
    now = [0]
    c, t = client(fake_transport, token(), task(), task(), token("fake-new-token"), task(),
                  reply({}, 401), token("fake-refetched-token"), task(), clock=lambda: now[0])
    c.get_task(91)
    now[0] = 3539
    c.get_task(91)
    assert len(t.calls) == 3
    now[0] = 3540
    c.get_task(91)
    assert len(t.calls) == 5
    c.get_task(91)
    assert len(t.calls) == 8
    assert t.calls[-1][2]["Authorization"] == "Bearer fake-refetched-token"


def test_401_only_once(fake_transport):
    c, t = client(fake_transport, token(), reply({}, 401), token(), reply({}, 401))
    with pytest.raises(SigningError, match="HTTP 401"):
        c.create_task({})
    assert len(t.calls) == 4


def test_error_body_never_surfaced(fake_transport, caplog):
    c, t = client(fake_transport, token(), reply({"error_code": 400220, "error_key": "email_format_invalid",
                  "error_message": EMAIL + CLIENT_SECRET + TOKEN, "email": EMAIL}, 400))
    with pytest.raises(SigningError) as caught:
        c.create_task({})
    assert "400220" in str(caught.value) and "email_format_invalid" in str(caught.value)
    assert_private(caught.value)
    assert all(value not in caplog.text for value in (CLIENT_SECRET, TOKEN, EMAIL))
    assert len(t.calls) == 2


@pytest.mark.parametrize("key", ["bad-key", "X" * 70, EMAIL, {}, None])
def test_error_key_allowlist(fake_transport, key):
    c, _ = client(fake_transport, token(), reply({"error_key": key, "error_code": True}, 400))
    with pytest.raises(SigningError) as caught:
        c.create_task({})
    assert "error_key" not in str(caught.value) and "error_code" not in str(caught.value)


def test_403_no_retry(fake_transport):
    c, t = client(fake_transport, token(), reply({"error_code": 403032}, 403))
    with pytest.raises(SigningQuotaExceeded, match="403032"):
        c.create_task({})
    assert len([r for r in t.calls if r[1].endswith("/v1/sign_tasks")]) == 1


@pytest.mark.parametrize("failure", [TransportError(EMAIL + CLIENT_SECRET + LINK), reply({}, 429), reply({}, 500)])
def test_create_never_retries(fake_transport, failure):
    sleeps = []
    c, t = client(fake_transport, token(), failure, sleep=sleeps.append)
    with pytest.raises(SigningError) as caught:
        c.create_task({})
    assert len(t.calls) == 2 and not sleeps
    assert_private(caught.value)
    if isinstance(failure, TransportError):
        assert "MAY have been created" in str(caught.value)
        assert "quote-sign status" in str(caught.value)
        assert caught.value.__context__ is None


@pytest.mark.parametrize("failure", [reply({}, 429), reply({}, 503), TransportError(LINK)])
def test_get_retry(fake_transport, failure):
    sleeps = []
    c, t = client(fake_transport, token(), failure, failure, task(), sleep=sleeps.append)
    assert c.get_task(91).stages == (StageStatus(1, "processing"), StageStatus(2, "initial"))
    assert sleeps == [1, 2] and len(t.calls) == 4


def test_status_discards_private_fields_and_unknowns(fake_transport):
    c, _ = client(fake_transport, token(), task("future", details=[{"sequence": 1, "status": EMAIL, "email": EMAIL}]))
    result = c.get_task(91)
    assert result.status == "unknown" and result.stages[0].status == "unknown"
    assert all(s not in repr(result) for s in ("@", "Private", "Owner"))


@pytest.mark.parametrize("link,download", [("http://download.example.invalid", None), (None, None),
                                          (LINK, HttpResponse(200, b"not PDF")),
                                          (LINK, HttpResponse(400, LINK.encode()))])
def test_download_failures_private(fake_transport, link, download):
    responses = [token(), reply({"data": {"task": link}})]
    if download is not None:
        responses.append(download)
    c, t = client(fake_transport, *responses)
    with pytest.raises(SigningError) as caught:
        c.download_signed(91)
    assert_private(caught.value)
    if download:
        assert t.calls[-1][2] == {}


def test_expired_download_link_403_is_not_quota(fake_transport):
    c, t = client(fake_transport, token(), reply({"data": {"task": LINK}}), HttpResponse(403, b"<Error/>"))
    with pytest.raises(SigningError) as caught:
        c.download_signed(91)
    assert not isinstance(caught.value, SigningQuotaExceeded)
    assert_private(caught.value)
    assert t.calls[-1][2] == {}


@pytest.mark.parametrize("data", [{}, {"data": None}, {"data": {"task_id": True}}, {"data": {"task_id": 0}}])
def test_bad_create_id(fake_transport, data):
    c, _ = client(fake_transport, token(), reply(data))
    with pytest.raises(SigningError):
        c.create_task({})


def test_get_exhaustion_and_unexpected_transport_text_discarded(fake_transport):
    sleeps = []
    c, t = client(fake_transport, token(), TransportError(LINK), TransportError(LINK), TransportError(LINK),
                  sleep=sleeps.append)
    with pytest.raises(SigningError) as caught:
        c.get_task(91)
    assert len(t.calls) == 4 and sleeps == [1, 2]
    assert caught.value.__context__ is None
    assert_private(caught.value)
    c, t = client(fake_transport, token(), RuntimeError(LINK + CLIENT_SECRET + EMAIL))
    with pytest.raises(SigningError) as caught:
        c.get_task(91)
    assert caught.value.__context__ is None
    assert_private(caught.value)


def test_cached_old_token_is_also_redacted(fake_transport):
    c, _ = client(fake_transport, token("old_token_private"), reply({}, 401), token("new_token_private"),
                  reply({"error_key": "old_token_private"}, 400))
    with pytest.raises(SigningError) as caught:
        c.create_task({})
    assert "old_token_private" not in str(caught.value)


def test_response_representation_hides_body():
    response = HttpResponse(400, (CLIENT_SECRET + EMAIL + LINK).encode())
    assert all(value not in repr(response) + str(response) for value in (CLIENT_SECRET, EMAIL, LINK))
