"""src/privacy/drop.py: DROP digests, the download ZIP, the status CSV and the API client.

The authority is the vendored spec, docs/legal/audit-2026-11/drop/databroker_api-v1.2.0.yaml.
Golden digests are computed HERE with hashlib/base64 from the standardized form written
out by hand, never by calling the code under test. The HTTP client talks to a real HTTP
server on loopback (http.server in a thread) that answers with the spec's responses: no
external call, no mocking library. Synthetic identifiers only.
"""
from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.privacy import drop

_KEY = "drop-test-key-not-real-0123456789"


def _b64sha(standardized: str) -> str:
    return base64.b64encode(hashlib.sha256(standardized.encode("utf-8")).digest()).decode()


def _id(n: int) -> str:
    return f"A7kP2xQ9{n:04d}"


def _h(seed: str) -> str:
    return _b64sha(f"seed:{seed}")


# ── Golden vectors ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("raw", "standardized"), [
    ("John.Doe+Tag@Example.COM", "john.doe+tag@example.com"),
    ("  john.doe@example.com  ", "john.doe@example.com"),
    ("a b@x.com", "ab@x.com"),                         # no-break space
    ("a b\t@\nX.org", "ab@x.org"),                      # em space, tab, newline
    ("　Foo.Bar+1@Bar.IO ", "foo.bar+1@bar.io"),    # ideographic, line separator
    ("first . last @ host.com", "first.last@host.com"),
])
def test_email_digest_matches_the_spec_rule(raw, standardized):
    assert drop.standardize_email(raw) == standardized
    assert drop.email_digest(raw) == _b64sha(standardized)


@pytest.mark.parametrize(("raw", "standardized"), [
    ("(206) 555-0100", "2065550100"),
    ("+1 206-555-0100", "2065550100"),
    ("1-206-555-0100", "2065550100"),
    ("206.555.0100 ext", "2065550100"),
    ("555-0100", "5550100"),                                 # fewer than 10: all digits
    ("+44 20 7946 0958", "2079460958"),                      # more than 10: the last 10
])
def test_phone_digest_matches_the_spec_rule(raw, standardized):
    assert drop.standardize_phone(raw) == standardized
    assert drop.phone_digest(raw) == _b64sha(standardized)


@pytest.mark.parametrize("raw", [None, "", "   ", "  \n"])
def test_an_email_that_standardizes_to_nothing_has_no_digest(raw):
    assert drop.email_digest(raw) is None


@pytest.mark.parametrize("raw", [None, "", "ext.", "٢٠٦"])  # Arabic-Indic digits
def test_a_phone_with_no_ascii_digits_has_no_digest(raw):
    assert drop.phone_digest(raw) is None


def test_the_digest_is_44_char_base64_of_32_bytes():
    d = drop.email_digest("x@y.z")
    assert len(d) == 44 and drop.is_hash(d) and len(base64.b64decode(d)) == 32
    assert not drop.is_hash("abc")
    assert not drop.is_hash(d + "\n")


# ── The download ZIP ───────────────────────────────────────────────────────────────
def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _csv(header: str, rows: list[tuple[str, ...]]) -> str:
    return "\r\n".join([header] + [",".join(r) for r in rows]) + "\r\n"


def test_a_download_keeps_email_phone_and_removed_and_counts_the_rest():
    blob = _zip({
        "20260312_4821_Email.csv": _csv("ID,Hash", [(_id(1), _h("e1")), (_id(2), _h("e2"))]),
        "20260312_4821_Phone.csv": _csv("Id,Hash", [(_id(3), _h("p1"))]),
        "20260312_4821_NDZ.csv": _csv("ID,Hash", [(_id(4), _h("n1")), (_id(5), _h("n2"))]),
        "20260312_4821_Removed.csv": _csv("ID,Hash,ListType", [
            (_id(9), _h("e9"), "Email"), (_id(8), _h("n8"), "NDZ")]),
    })
    parsed = drop.parse_download(blob)
    assert sorted(parsed.items) == [("email", _id(1), _h("e1")), ("email", _id(2), _h("e2")),
                                    ("phone", _id(3), _h("p1"))]
    assert parsed.removed == [("email", _id(9), _h("e9"))]
    assert parsed.list_counts == {"email": 2, "phone": 1, "removed": 1}
    assert parsed.ignored_counts == {"NDZ": 2, "removed_NDZ": 1}
    assert parsed.list_files == {"email": "20260312_4821_Email.csv",
                                 "phone": "20260312_4821_Phone.csv",
                                 "removed": "20260312_4821_Removed.csv"}


@pytest.mark.parametrize("header", ["ID,Hash", "Id,Hash", "id,hash", "﻿ID,Hash"])
def test_the_header_is_accepted_in_either_spelling_the_spec_shows(header):
    parsed = drop.parse_download(_zip({"20260312_4821_Email.csv":
                                       _csv(header, [(_id(1), _h("a"))])}))
    assert parsed.list_counts == {"email": 1}


def test_a_header_only_file_means_no_new_records():
    parsed = drop.parse_download(_zip({"20260312_4821_Email.csv": "ID,Hash\r\n"}))
    assert parsed.items == [] and parsed.list_counts == {"email": 0}


def test_a_repeated_row_is_kept_once():
    rows = [(_id(1), _h("a")), (_id(1), _h("a"))]
    parsed = drop.parse_download(_zip({"20260312_4821_Email.csv": _csv("ID,Hash", rows)}))
    assert parsed.items == [("email", _id(1), _h("a"))]


_GOOD = _csv("ID,Hash", [(_id(1), _h("a"))])


@pytest.mark.parametrize("files", [
    {"../20260312_4821_Email.csv": _GOOD},                       # traversal
    {"lists/20260312_4821_Email.csv": _GOOD},                    # a path
    {"C:20260312_4821_Email.csv": _GOOD},
    {"20260312_4821_Email.csv": _GOOD, "inner.zip": "PK"},       # nested archive
    {"notes.txt": "hello"},
    {"20260312_4821_Email.csv": _GOOD, "20260312_4821_Email_part01.csv": _GOOD},
    {"20260312_4821_Email.csv": _csv("Email,Hash", [(_id(1), _h("a"))])},
    {"20260312_4821_Email.csv": _csv("ID,Hash,Extra", [(_id(1), _h("a"), "x")])},
    {"20260312_4821_Email.csv": _csv("ID,Hash", [(_id(1), "not-a-hash")])},
    {"20260312_4821_Email.csv": _csv("ID,Hash", [(_id(1), "A" * 44)])},
    {"20260312_4821_Email.csv": _csv("ID,Hash", [("680", _h("a"))])},
    {"20260312_4821_Email.csv": _csv("ID,Hash", [(_id(1), _h("a")), (_id(1), _h("b"))])},
    {"20260312_4821_Email.csv": _csv("ID,Hash", [(_id(1), _h("a"), "extra")])},
    {"20260312_4821_Removed.csv": _csv("ID,Hash", [(_id(1), _h("a"))])},
    {"20260312_4821_Removed.csv": _csv("ID,Hash,ListType", [(_id(1), _h("a"), "Fax")])},
    {"20260312_4821_NDZ.csv": _csv("ID,Hash", [(_id(1), "bad")])},
])
def test_a_download_the_spec_does_not_describe_is_refused(files):
    with pytest.raises(drop.DropFormatError):
        drop.parse_download(_zip(files))


def test_bounds_are_enforced():
    rows = [(_id(i), _h(str(i))) for i in range(50)]
    blob = _zip({"20260312_4821_Email.csv": _csv("ID,Hash", rows)})
    for limits in (drop.Limits(max_zip_bytes=len(blob) - 1), drop.Limits(max_rows=49),
                   drop.Limits(max_uncompressed_bytes=500), drop.Limits(max_members=0),
                   drop.Limits(max_line_chars=20)):
        with pytest.raises(drop.DropFormatError):
            drop.parse_download(blob, limits)
    assert len(drop.parse_download(blob).items) == 50
    with pytest.raises(drop.DropFormatError):
        drop.parse_download(b"this is not a zip")


def test_a_refusal_never_echoes_the_data():
    secret = _h("secret-hash")
    with pytest.raises(drop.DropFormatError) as err:
        drop.parse_download(_zip({"20260312_4821_Email.csv":
                                  _csv("ID,Hash", [(_id(1), secret), (_id(1), _h("b"))])}))
    assert secret not in str(err.value) and _id(1) not in str(err.value)


# ── The status CSV ─────────────────────────────────────────────────────────────────
def test_the_status_csv_is_exactly_the_spec_format():
    out = drop.status_csv([(_id(1), 2), (_id(2), 3), (_id(3), 4), (_id(4), 5)])
    assert out == (b"Id,Status\r\nA7kP2xQ90001,2\r\nA7kP2xQ90002,3\r\n"
                   b"A7kP2xQ90003,4\r\nA7kP2xQ90004,5\r\n")
    assert drop.status_csv([]) == b"Id,Status\r\n"


@pytest.mark.parametrize("rows", [[(_id(1), 1)], [(_id(1), 6)], [(_id(1), True)],
                                  [(_id(1), "2")], [("680", 5)], [(_id(1), 2), (_id(1), 3)]])
def test_the_status_csv_refuses_what_drop_would_reject(rows):
    with pytest.raises(ValueError):
        drop.status_csv(rows)


def test_the_status_file_reuses_the_downloaded_name():
    assert drop.status_file_name("20260312_4821_Email.csv") == "20260312_4821_Email.csv"
    assert (drop.status_file_name("20260312_4821_Phone.csv", "part01")
            == "20260312_4821_Phone_part01.csv")
    for bad, suffix in (("20260312_4821_Removed.csv", None), ("x.csv", None),
                        ("20260312_4821_Email_part01.csv", None),
                        ("20260312_4821_Email.csv", "part_01"),
                        ("20260312_4821_Email.csv", "x" * 11)):
        with pytest.raises(ValueError):
            drop.status_file_name(bad, suffix)


# ── The API client against a loopback server ───────────────────────────────────────
class _Handler(BaseHTTPRequestHandler):
    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        status, headers, payload, delay = self.server.answers.pop(0)
        if delay:
            time.sleep(delay)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _serve

    def log_message(self, *args):
        pass


class DropServer:
    """A local HTTP server answering with queued (status, headers, body, delay)."""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.seen, self.httpd.answers = [], []
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def seen(self):
        return self.httpd.seen

    def answer(self, status: int, payload: bytes = b"", delay: float = 0, **headers):
        self.httpd.answers.append(
            (status, {k.replace("_", "-"): v for k, v in headers.items()}, payload, delay))

    def json(self, status: int, body: str, **headers):
        self.answer(status, body.encode(), Content_Type="application/json", **headers)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = DropServer()
    try:
        yield s
    finally:
        s.close()


def _client(server, **kw) -> drop.DropClient:
    return drop.DropClient(_KEY, server.url, environment="test", **kw)


def test_download_returns_the_zip_and_sends_the_key_and_accept_header(server):
    blob = _zip({"20260312_4821_Email.csv": _GOOD})
    server.answer(200, blob, Content_Type="application/zip",
                  Content_Disposition='attachment; filename="20260312_4821_DROP.zip"')
    got = _client(server).download()
    assert got == drop.ZipReady("20260312_4821_DROP.zip", blob)
    method, path, headers, _ = server.seen[0]
    assert (method, path) == ("GET", "/data/download")
    assert headers["X-API-KEY"] == _KEY
    assert headers["Accept"] == "application/zip, application/json"


def test_download_no_new_data_and_preparing(server):
    server.json(200, '{"message": "No new consumer request data or removed identifiers '
                     'are available since your last completed download."}')
    server.json(202, '{"message": "No download package is available yet."}', Retry_After="60")
    server.json(202, '{"message": "preparing"}')
    client = _client(server)
    assert client.download() == drop.NoNewData()
    assert client.download() == drop.Preparing(60)
    assert client.download() == drop.Preparing(60)  # no Retry-After: a minute


def test_a_429_is_retry_later_after_30_seconds(server):
    server.json(429, '{"message": "Too many requests. Wait and try again."}', Retry_After="30")
    server.json(429, '{"message": "Too many requests."}')
    for _ in range(2):
        with pytest.raises(drop.DropRetryLaterError) as err:
            _client(server).download()
        assert err.value.retry_after == 30 and err.value.status == 429


@pytest.mark.parametrize(("status", "error"), [
    (401, drop.DropAuthError), (403, drop.DropForbiddenError), (404, drop.DropNotFoundError),
    (409, drop.DropConflictError), (500, drop.DropRetryLaterError), (302, drop.DropProtocolError),
    (418, drop.DropProtocolError)])
def test_errors_are_typed_and_never_carry_the_key(server, status, error):
    server.json(status, '{"message": "x"}', Location="http://127.0.0.1:1/elsewhere")
    client = _client(server)
    with pytest.raises(error) as err:
        client.download()
    assert err.value.status == status
    assert _KEY not in str(err.value) and _KEY not in repr(client)
    assert len(server.seen) == 1  # no retry, no redirect followed


def test_a_200_that_is_neither_zip_nor_json_is_refused(server):
    server.answer(200, b"<html>maintenance</html>", Content_Type="text/html")
    with pytest.raises(drop.DropProtocolError):
        _client(server).download()


def test_an_oversize_response_is_refused(server):
    server.answer(200, b"PK\x03\x04" + b"0" * 5000, Content_Type="application/zip")
    with pytest.raises(drop.DropProtocolError):
        _client(server, limits=drop.Limits(max_zip_bytes=1000)).download()


def test_a_slow_server_is_retry_later(server):
    server.json(200, "{}", delay=1.5)
    with pytest.raises(drop.DropRetryLaterError):
        _client(server, read_timeout=0.3).download()


def test_upload_and_amend_send_multipart_files(server):
    accepted = ('{"message": "Upload received.", "acceptedCount": 1, "rejectedCount": 0, '
                '"accepted": [{"fileName": "20260312_4821_Email.csv", "fileSizeBytes": 40}], '
                '"rejected": []}')
    server.json(202, accepted)
    server.json(202, accepted)
    body = drop.status_csv([(_id(1), 5)])
    client = _client(server)
    assert client.upload([("20260312_4821_Email.csv", body)]) == drop.UploadResult(
        accepted=("20260312_4821_Email.csv",), rejected=())
    client.amend([("20260312_4821_Email.csv", body)])
    for (method, path, headers, sent), want in zip(server.seen, ("/data/upload", "/data/amend"),
                                                   strict=True):
        assert (method, path) == ("POST", want)
        assert headers["X-API-KEY"] == _KEY and headers["Accept"] == "application/json"
        assert headers["Content-Type"].startswith("multipart/form-data")
        assert b'name="files"; filename="20260312_4821_Email.csv"' in sent
        assert b"Content-Type: text/csv" in sent and body in sent


def test_an_upload_drop_rejects_is_typed_with_the_reasons(server):
    server.json(400, '{"message": "Upload request is invalid.", "acceptedCount": 0, '
                     '"rejectedCount": 1, "accepted": [], "rejected": [{"fileName": '
                     '"20260312_4821_Email.csv", "message": "Invalid CSV header. Expected: '
                     'Id,Status."}]}')
    with pytest.raises(drop.DropRejectedError) as err:
        _client(server).upload([("20260312_4821_Email.csv", b"Id,Status\r\n")])
    assert err.value.result.rejected == (
        ("20260312_4821_Email.csv", "Invalid CSV header. Expected: Id,Status."),)


def test_upload_refuses_a_name_drop_did_not_issue(server):
    with pytest.raises(ValueError):
        _client(server).upload([("statuses.csv", b"Id,Status\r\n")])
    assert server.seen == []


@pytest.mark.parametrize(("url", "env", "ok"), [
    ("https://api.drop.privacy.ca.gov", "production", True),
    ("https://api.drop.privacy.ca.gov/", "production", True),
    ("https://api.drop.privacy.ca.gov/sandbox", "production", True),
    ("http://api.drop.privacy.ca.gov", "production", False),
    ("https://api.drop.privacy.ca.gov.evil.example", "production", False),
    ("https://api.drop.privacy.ca.gov:8443", "production", False),
    ("https://user@api.drop.privacy.ca.gov", "production", False),
    ("https://api.drop.privacy.ca.gov/other", "production", False),
    ("http://127.0.0.1:8080", "production", False),
    ("http://127.0.0.1:8080", "staging", False),
    ("http://127.0.0.1:8080", "test", True),
    ("http://10.0.0.5:8080", "test", False),
])
def test_only_drop_itself_receives_the_key(url, env, ok):
    if ok:
        assert drop.check_base_url(url, env) == url.rstrip("/")
    else:
        with pytest.raises(ValueError):
            drop.check_base_url(url, env)


def test_the_client_needs_a_key():
    with pytest.raises(ValueError):
        drop.DropClient("", drop.PRODUCTION_BASE_URL, environment="production")
