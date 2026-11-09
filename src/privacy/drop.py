"""California DROP (Delete Act) protocol: digests, the download ZIP, the status CSV, the API.

DORMANT FOUNDATION (tasks/todo-privacy-p9-drop.md, "Foundation now, rest later"). Nothing
here reads, withholds, erases or reports a lead; whether BridgeLeads must process DROP at
all is counsel's decision (D8). The authority is the vendored official spec,
docs/legal/audit-2026-11/drop/databroker_api-v1.2.0.yaml (DROP Data Broker API 1.2.0).

Digests: SHA-256 of the UTF-8 bytes of the standardized value, Base64 encoded.
  Email  remove all whitespace (Unicode whitespace included), then lowercase. Dots, plus
         signs and every other character are kept.
  Phone  keep digits only, then the last 10 digits, or all of them if fewer than 10.
A value that standardizes to nothing has no digest (None). We hold no DOB, VIN, MAID or
CTV IDs, so only the Email and Phone lists can ever match; the others are counted and
ignored.

Nothing in this module logs or puts into an exception message the API key, an identifier
or a hash.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

EMAIL = "email"
PHONE = "phone"
REMOVED = "removed"
LIST_TYPES = (EMAIL, PHONE)
STATUS_CODES = frozenset({2, 3, 4, 5})  # Exempted, Deleted, Opted out, Not found

# The spec's DataType enum, as it appears in file names; Removed is a file type only.
_KEPT = {"Email": EMAIL, "Phone": PHONE}
_IGNORED = ("NDZ", "MAID", "NameVIN", "CTVID")
_FILE_NAME = re.compile(
    r"^(?P<prefix>[0-9]{8}_[0-9]{4})_(?P<type>NDZ|Email|Phone|MAID|NameVIN|CTVID|Removed)"
    r"(?:_(?P<suffix>[A-Za-z0-9]{1,10}))?\.csv$")
_WORK_ITEM_ID = re.compile(r"^[A-Za-z0-9]{12}$")
_HASH = re.compile(r"^[A-Za-z0-9+/]{43}=$")
_ZIP_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,96}\.zip$")

PRODUCTION_BASE_URL = "https://api.drop.privacy.ca.gov"
SANDBOX_BASE_URL = "https://api.drop.privacy.ca.gov/sandbox"
_DROP_HOST = "api.drop.privacy.ca.gov"
_DROP_PATHS = ("", "/sandbox")
# A loopback http base URL is accepted only in these environments (a local DROP stand-in
# for development and the test suite); production accepts the two official URLs only.
_LOCAL_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


# ── Standardization and digests ────────────────────────────────────────────────────
def standardize_email(value: str | None) -> str | None:
    if not value:
        return None
    out = "".join(ch for ch in value if not ch.isspace()).lower()
    return out or None


def standardize_phone(value: str | None) -> str | None:
    if not value:
        return None
    # ASCII digits only: str.isdigit() would also keep other scripts' digits.
    digits = "".join(ch for ch in value if "0" <= ch <= "9")
    return digits[-10:] or None


def digest(standardized: str) -> str:
    """Base64(SHA-256(UTF-8)) of an already standardized value."""
    return base64.b64encode(hashlib.sha256(standardized.encode("utf-8")).digest()).decode("ascii")


def email_digest(value: str | None) -> str | None:
    s = standardize_email(value)
    return digest(s) if s else None


def phone_digest(value: str | None) -> str | None:
    s = standardize_phone(value)
    return digest(s) if s else None


def is_hash(value: str) -> bool:
    if not _HASH.fullmatch(value):
        return False
    try:
        return len(base64.b64decode(value, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


# ── The download ZIP ───────────────────────────────────────────────────────────────
class DropFormatError(ValueError):
    """The download is not what the spec describes. The message never carries data."""


@dataclass(frozen=True)
class Limits:
    """Bounds on a download. Members are streamed row by row, so memory is bounded by
    the rows kept (max_rows), not by the archive's size."""
    max_zip_bytes: int = 64 * 1024 * 1024
    max_members: int = 16
    max_uncompressed_bytes: int = 256 * 1024 * 1024   # all members together
    max_ratio: int = 100                               # per member, decompressed:compressed
    ratio_floor_bytes: int = 64 * 1024                 # tiny members are exempt from the ratio
    max_rows: int = 2_000_000                          # all members together
    max_line_chars: int = 256


@dataclass
class ParsedDownload:
    """Email and Phone identifiers, and Removed rows of those two lists, as
    (list_type, drop_id, hash) tuples. Counts are per list (email, phone, removed) and,
    for what we cannot match, per DROP DataType (Removed rows of those: removed_<Type>)."""
    items: list[tuple[str, str, str]] = field(default_factory=list)
    removed: list[tuple[str, str, str]] = field(default_factory=list)
    list_counts: dict[str, int] = field(default_factory=dict)
    ignored_counts: dict[str, int] = field(default_factory=dict)
    list_files: dict[str, str] = field(default_factory=dict)


class _Budget:
    """Decompressed bytes and rows left for the whole archive."""

    def __init__(self, limits: Limits):
        self.bytes = limits.max_uncompressed_bytes
        self.rows = limits.max_rows


def _ratio_cap(info: zipfile.ZipInfo, limits: Limits) -> int:
    return max(limits.max_ratio * max(info.compress_size, 1), limits.ratio_floor_bytes)


class _CappedReader(io.RawIOBase):
    """A member's decompressed bytes, counted as they are read: past the archive budget
    or the member's compression-ratio cap the read is refused (the sizes the archive
    declares are not trusted)."""

    def __init__(self, raw, info: zipfile.ZipInfo, budget: _Budget, limits: Limits):
        self._raw = raw
        self._budget = budget
        self._read = 0
        self._cap = _ratio_cap(info, limits)

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        n = self._raw.readinto(buf)
        self._read += n
        self._budget.bytes -= n
        if self._budget.bytes < 0:
            raise DropFormatError("download exceeds the uncompressed size limit")
        if self._read > self._cap:
            raise DropFormatError("a file in the download exceeds the compression-ratio limit")
        return n

    def close(self) -> None:
        self._raw.close()
        super().close()


def _lines(text_stream, name: str, limits: Limits):
    """Lines of at most max_line_chars (line end excluded), read one at a time."""
    while True:
        try:
            line = text_stream.readline(limits.max_line_chars + 3)
        except UnicodeDecodeError:
            raise DropFormatError(f"{name}: not UTF-8") from None
        if not line:
            return
        if len(line.rstrip("\r\n")) > limits.max_line_chars:
            raise DropFormatError(f"{name}: a line exceeds the length limit")
        yield line


def _member_rows(zf: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _Budget,
                 limits: Limits):
    """(lowercased header, row iterator) for one member, streamed from a bounded reader;
    each row counts against the archive's row budget."""
    name = info.filename
    # Cheap early refusal on the declared size; the streamed count is the real limit.
    if info.file_size > budget.bytes or info.file_size > _ratio_cap(info, limits):
        raise DropFormatError("a file in the download exceeds the size or ratio limit")
    stream = io.TextIOWrapper(
        io.BufferedReader(_CappedReader(zf.open(info), info, budget, limits), 64 * 1024),
        encoding="utf-8-sig", newline="")
    reader = csv.reader(_lines(stream, name, limits), strict=True)

    def _next() -> list[str] | None:
        try:
            for row in reader:
                if row:
                    return row
        except csv.Error:
            raise DropFormatError(f"{name}: malformed CSV") from None
        except (zipfile.BadZipFile, EOFError):
            raise DropFormatError(f"{name}: unreadable") from None
        return None

    header = _next()
    if header is None:
        stream.close()
        raise DropFormatError(f"{name}: no header row")

    def _rows():
        try:
            while (row := _next()) is not None:
                budget.rows -= 1
                if budget.rows < 0:
                    raise DropFormatError("download exceeds the row limit")
                yield row
        finally:
            stream.close()

    return [h.strip().lower() for h in header], _rows()


def _check_row(row: list[str], width: int, name: str) -> tuple[str, str]:
    if len(row) != width:
        raise DropFormatError(f"{name}: a row has the wrong number of columns")
    drop_id, hash_ = row[0].strip(), row[1].strip()
    if not _WORK_ITEM_ID.fullmatch(drop_id):
        raise DropFormatError(f"{name}: a row has a malformed Id")
    if not is_hash(hash_):
        raise DropFormatError(f"{name}: a row has a malformed Hash")
    return drop_id, hash_


def parse_download(blob: bytes, limits: Limits = Limits()) -> ParsedDownload:
    """Parse a DROP download ZIP in memory (nothing is extracted to disk), streaming each
    CSV row by row from a bounded reader.

    Refused (DropFormatError): an oversize archive, too many members, more decompressed
    bytes than the budget or a member past the compression-ratio cap (zip bombs), a
    member name that is a path (traversal), a directory or anything that is not a DROP
    CSV name (nested archives included), two files of one DataType, a header other than
    Id,Hash in any case (the spec writes `ID,Hash` for downloads, `Id,Status` for
    uploads) or ID,Hash,ListType for Removed, a malformed Id or Hash, one Id with two
    hashes (per list, Removed included), an over-long line, or more rows than the limit.
    """
    if len(blob) > limits.max_zip_bytes:
        raise DropFormatError("download exceeds the ZIP size limit")
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise DropFormatError("download is not a ZIP archive") from None
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise DropFormatError("download has too many files")
    out = ParsedDownload()
    seen_types: set[str] = set()
    budget = _Budget(limits)
    for info in infos:
        name = info.filename
        if ("/" in name or "\\" in name or ".." in name or ":" in name
                or info.is_dir() or not _FILE_NAME.fullmatch(name)):
            # Never echo the name back: it is attacker-controlled text.
            raise DropFormatError("download contains a file that is not a DROP CSV")
        data_type = _FILE_NAME.fullmatch(name).group("type")
        if data_type in seen_types:
            raise DropFormatError(f"download contains two {data_type} files")
        seen_types.add(data_type)
        header, rows = _member_rows(zf, info, budget, limits)
        if data_type == "Removed":
            if header != ["id", "hash", "listtype"]:
                raise DropFormatError(f"{name}: header is not ID,Hash,ListType")
            # One Id, one hash, per list type: the same rule as the lists themselves.
            removed_by_id: dict[tuple[str, str], str] = {}
            for row in rows:
                drop_id, hash_ = _check_row(row, 3, name)
                list_type = row[2].strip()
                if list_type not in _KEPT and list_type not in _IGNORED:
                    raise DropFormatError(f"{name}: a row has an unknown ListType")
                if removed_by_id.setdefault((list_type, drop_id), hash_) != hash_:
                    raise DropFormatError(f"{name}: one Id carries two hashes")
            kept = 0
            for (list_type, drop_id), hash_ in removed_by_id.items():
                if list_type in _KEPT:
                    out.removed.append((_KEPT[list_type], drop_id, hash_))
                    kept += 1
                else:
                    key = f"removed_{list_type}"
                    out.ignored_counts[key] = out.ignored_counts.get(key, 0) + 1
            out.list_counts[REMOVED] = kept
            out.list_files[REMOVED] = name
            continue
        if header != ["id", "hash"]:
            raise DropFormatError(f"{name}: header is not Id,Hash")
        if data_type in _IGNORED:
            count = 0
            for row in rows:
                _check_row(row, 2, name)
                count += 1
            out.ignored_counts[data_type] = count
            continue
        list_type = _KEPT[data_type]
        by_id: dict[str, str] = {}
        for row in rows:
            drop_id, hash_ = _check_row(row, 2, name)
            if by_id.setdefault(drop_id, hash_) != hash_:
                raise DropFormatError(f"{name}: one Id carries two hashes")
        out.items.extend((list_type, i, h) for i, h in by_id.items())
        out.list_counts[list_type] = len(by_id)
        out.list_files[list_type] = name
    return out


# ── The status CSV ─────────────────────────────────────────────────────────────────
def status_file_name(downloaded: str, suffix: str | None = None) -> str:
    """The upload name for statuses of a downloaded Email/Phone CSV: the same name, or
    with ``_<suffix>`` (1-10 letters/digits) before .csv for a second file."""
    m = _FILE_NAME.fullmatch(downloaded)
    if not m or m.group("suffix") or m.group("type") == "Removed":
        raise ValueError("not a downloaded DROP list file name")
    if suffix is None:
        return downloaded
    if not re.fullmatch(r"[A-Za-z0-9]{1,10}", suffix):
        raise ValueError("suffix must be 1-10 letters or digits")
    return f"{m.group('prefix')}_{m.group('type')}_{suffix}.csv"


def status_csv(rows: list[tuple[str, int]]) -> bytes:
    """``Id,Status`` then one row per work item, UTF-8, CRLF line ends (RFC 4180)."""
    seen: set[str] = set()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["Id", "Status"])
    for drop_id, status in rows:
        if not isinstance(drop_id, str) or not _WORK_ITEM_ID.fullmatch(drop_id):
            raise ValueError("malformed work item Id")
        if type(status) is not int or status not in STATUS_CODES:
            raise ValueError("status must be 2, 3, 4 or 5")
        if drop_id in seen:
            raise ValueError("a work item Id appears twice")
        seen.add(drop_id)
        writer.writerow([drop_id, status])
    return buf.getvalue().encode("utf-8")


# ── The API client ─────────────────────────────────────────────────────────────────
class DropError(Exception):
    """A DROP API call failed. ``status`` is the HTTP status (None: no response)."""

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


class DropRetryLaterError(DropError):
    """429, a 5xx, a timeout or a lost connection: call again after ``retry_after`` s."""

    def __init__(self, reason: str, retry_after: int, status: int | None = None):
        super().__init__(reason, status)
        self.retry_after = retry_after


class DropAuthError(DropError):
    """401: the API key is missing, invalid or was replaced in the portal."""


class DropForbiddenError(DropError):
    """403: the account is not eligible, or no list is selected."""


class DropNotFoundError(DropError):
    """404: wrong base URL or path."""


class DropConflictError(DropError):
    """409: e.g. the previous download's batch is not complete (statuses owed)."""


class DropRejectedError(DropError):
    """400 on upload/amend: no file was accepted. ``result`` lists the rejections."""

    def __init__(self, reason: str, result: UploadResult | None, status: int = 400):
        super().__init__(reason, status)
        self.result = result


class DropProtocolError(DropError):
    """A response the spec does not describe (status, type, size or redirect)."""


@dataclass(frozen=True)
class ZipReady:
    file_name: str
    content: bytes


@dataclass(frozen=True)
class NoNewData:
    pass


@dataclass(frozen=True)
class Preparing:
    retry_after: int


@dataclass(frozen=True)
class UploadResult:
    accepted: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]   # (file name, DROP's reason)


_STATUS_ERRORS = {401: DropAuthError, 403: DropForbiddenError, 404: DropNotFoundError,
                  409: DropConflictError}
_RETRY_DEFAULT = 30
_RETRY_MAX = 3600


def check_base_url(url: str, environment: str) -> str:
    """The base URL, if it is one of the spec's two servers (or loopback http in a
    development/test environment). Anything else is refused: the API key goes with
    every request."""
    parts = urlsplit(url.rstrip("/"))
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("DROP_BASE_URL must be a plain base URL")
    if parts.scheme == "https" and parts.hostname == _DROP_HOST and parts.port is None \
            and parts.path in _DROP_PATHS:
        return url.rstrip("/")
    if (parts.scheme == "http" and parts.hostname in _LOOPBACK and parts.path == ""
            and environment.strip().lower() in _LOCAL_ENVIRONMENTS):
        return url.rstrip("/")
    raise ValueError("DROP_BASE_URL must be the DROP production or sandbox URL")


def _retry_after(resp: requests.Response, default: int = _RETRY_DEFAULT) -> int:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if raw.isdigit() and 1 <= int(raw) <= _RETRY_MAX:
        return int(raw)
    return default


def _zip_name(resp: requests.Response) -> str:
    disposition = resp.headers.get("Content-Disposition") or ""
    m = re.search(r'filename="?([^";]+)"?', disposition)
    if m and _ZIP_NAME.fullmatch(m.group(1)):
        return m.group(1)
    return f"drop_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.zip"


def _upload_result(resp: requests.Response) -> UploadResult | None:
    try:
        body = resp.json()
        return UploadResult(
            accepted=tuple(str(a["fileName"]) for a in body.get("accepted") or []),
            rejected=tuple((str(r["fileName"]), str(r.get("message", ""))[:200])
                           for r in body.get("rejected") or []))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class DropClient:
    """GET /data/download, POST /data/upload and POST /data/amend, exactly as the spec
    describes them. No retries here: 429, 5xx, timeouts and lost connections raise
    DropRetryLaterError for the caller to schedule; other 4xx raise their typed error.
    Redirects are refused and ambient proxies ignored (the key must reach DROP only)."""

    def __init__(self, api_key: str, base_url: str, *, environment: str,
                 connect_timeout: float = 10.0, read_timeout: float = 120.0,
                 limits: Limits = Limits()):
        if not api_key:
            raise ValueError("DROP API key is not set")
        self._base = check_base_url(base_url, environment)
        self._key = api_key
        self._timeout = (connect_timeout, read_timeout)
        self._limits = limits
        self._session = requests.Session()
        self._session.trust_env = False

    def __repr__(self) -> str:  # never the key
        return f"DropClient(base_url={self._base!r})"

    def close(self) -> None:
        self._session.close()

    def _request(self, method: str, path: str, *, accept: str, **kwargs) -> requests.Response:
        try:
            resp = self._session.request(
                method, self._base + path, headers={"X-API-KEY": self._key, "Accept": accept},
                timeout=self._timeout, allow_redirects=False, stream=True, **kwargs)
        except requests.Timeout:
            raise DropRetryLaterError("DROP did not answer in time", _RETRY_DEFAULT) from None
        except requests.ConnectionError:
            raise DropRetryLaterError("could not connect to DROP", _RETRY_DEFAULT) from None
        try:
            self._read_capped(resp)
        except BaseException:
            resp.close()
            raise
        return resp

    def _read_capped(self, resp: requests.Response) -> None:
        cap = self._limits.max_zip_bytes
        declared = resp.headers.get("Content-Length") or ""
        if declared.isdigit() and int(declared) > cap:
            raise DropProtocolError("DROP response exceeds the size limit", resp.status_code)
        body = bytearray()
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                body += chunk
                if len(body) > cap:
                    raise DropProtocolError("DROP response exceeds the size limit",
                                            resp.status_code)
        except requests.RequestException:
            raise DropRetryLaterError("the DROP response was cut off", _RETRY_DEFAULT,
                                 resp.status_code) from None
        resp._content = bytes(body)  # noqa: SLF001 -- backs .content / .json()
        resp._content_consumed = True  # noqa: SLF001

    @staticmethod
    def _raise_for(resp: requests.Response) -> None:
        code = resp.status_code
        if code == 429:
            raise DropRetryLaterError("DROP rate limit (429)", _retry_after(resp), code)
        if code >= 500:
            raise DropRetryLaterError(f"DROP server error ({code})", _retry_after(resp, 60), code)
        if 300 <= code < 400:
            raise DropProtocolError(f"DROP answered with a redirect ({code})", code)
        if code in _STATUS_ERRORS:
            raise _STATUS_ERRORS[code](f"DROP refused the request ({code})", code)
        raise DropProtocolError(f"unexpected DROP response ({code})", code)

    def download(self) -> ZipReady | NoNewData | Preparing:
        resp = self._request("GET", "/data/download",
                             accept="application/zip, application/json")
        try:
            if resp.status_code == 202:
                return Preparing(_retry_after(resp, 60))
            if resp.status_code != 200:
                self._raise_for(resp)
            body = resp.content
            if body[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
                return ZipReady(_zip_name(resp), body)
            if "json" in (resp.headers.get("Content-Type") or "").lower():
                try:
                    json.loads(body)
                except ValueError:
                    raise DropProtocolError("DROP sent malformed JSON", 200) from None
                return NoNewData()
            raise DropProtocolError("DROP 200 is neither a ZIP nor JSON", 200)
        finally:
            resp.close()

    def _send(self, path: str, files: list[tuple[str, bytes]]) -> UploadResult:
        if not files:
            raise ValueError("no files to send")
        for name, _ in files:
            m = _FILE_NAME.fullmatch(name)
            if not m or m.group("type") == "Removed":
                raise ValueError("a status file must use a downloaded list file name")
        resp = self._request(
            "POST", path, accept="application/json",
            files=[("files", (name, data, "text/csv")) for name, data in files])
        try:
            if resp.status_code == 202:
                result = _upload_result(resp)
                if result is None:
                    raise DropProtocolError("DROP 202 without an upload result", 202)
                return result
            if resp.status_code == 400:
                raise DropRejectedError("DROP accepted none of the files (400)",
                                   _upload_result(resp))
            self._raise_for(resp)
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            resp.close()

    def upload(self, files: list[tuple[str, bytes]]) -> UploadResult:
        """POST /data/upload. A 202 means queued for validation, NOT accepted."""
        return self._send("/data/upload", files)

    def amend(self, files: list[tuple[str, bytes]]) -> UploadResult:
        """POST /data/amend: same format, corrects statuses already submitted."""
        return self._send("/data/amend", files)
