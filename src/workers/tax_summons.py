"""Operator review of a county Treasurer's tax-foreclosure Notice and Summons (migration 173).

Douglas WA files one summons a year and re-uploads the scanned PDF as parcels redeem,
stamping those rows REDEEMED with a strike line OCR cannot read. So every distinct
digest is a version an operator reviews by hand:

  claim_watch / record_observation
                 the watcher (Phase B) claims the county (a fencing token) BEFORE it
                 fetches, then records what it found: the copy it confirmed the county
                 serves (a new digest becomes a pending version) or a hold
                 (link_missing, page_failed, download_failed, ambiguous, rollback). A late
                 run holding a stale token is refused, so it never overwrites a newer one.
  serving_status the Phase C predicate: the county's current copy is published and was
                 confirmed recently by the database clock, else a typed hold.
  set_manifest   the operator's reading of the PDF: its case number and how many rows
                 each exhibit page holds. A load must account for exactly those rows.
  draft_rows     a starting file: the case's last published rows, each marked
                 unconfirmed, so every row is re-checked against the new PDF. The file
                 names the case and manifest it was drafted under; a load needs both
                 to be still current.
  load_rows      the operator's confirmed rows, stored under a new revision (rows are
                 never edited or deleted).
  publish        approves one named revision, so what is published is what the operator
                 saw; refused unless the version is the copy the county serves now.

A parcel the county stamped redeemed stays out of later versions of the same case until
a published version reinstates it with a reason code: the latest published decision on
a parcel (redeemed or eligible) is the one that binds.

Every step takes one advisory lock per county first, so a watcher opening a new digest,
a load and a publish never interleave, and every row read under it is re-read from the
database (populate_existing), never from a session's cache. Events carry codes,
revisions and parcel numbers only; names and addresses never reach the audit, an error
message or the CLI's output (except `show`, for the operator's eyes).
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, text

from src.db.models import TaxSummonsEvent as Event
from src.db.models import TaxSummonsObservation as Observation
from src.db.models import TaxSummonsRow as Row
from src.db.models import TaxSummonsVersion as Version

ELIGIBILITY = ("eligible", "redeemed", "unreadable", "excluded")
EXCLUDE_REASONS = ("duplicate", "not_a_parcel", "privacy_request", "entered_in_error")
REINSTATE_REASONS = ("redemption_reversed", "entered_in_error")

# Only counties whose summons format an operator has checked. A county is added here
# with its parcel shape, never inferred.
_PARCEL = {("douglas", "WA"): re.compile(r"\d{11}", re.ASCII)}
_CASE = re.compile(r"\d{2}-\d-\d{5}-\d{2}", re.ASCII)  # Washington Superior Court case number
_LOCK_CLASS = 7173  # pg_advisory_xact_lock(class, key): tax summons review, per county

_ROW_KEYS = {"page", "row", "parcel", "vested_owner", "taxpayer", "property_address",
             "mailing_address", "amount", "tax_years", "eligibility", "confirmed", "reason",
             "reinstate_reason"}
_TEXT_KEYS = ("vested_owner", "taxpayer", "property_address", "mailing_address")
_MAX_AMOUNT = Decimal("10000000")
_AMOUNT = re.compile(r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
_MAX_PAGE, _MAX_ROWS_PER_PAGE, _MAX_ROWS = 9999, 50, 500

# A watcher check that did not confirm the county's copy. Each holds the county's tax
# leads at once (Phase C), whatever the last confirmed copy was.
HOLD_STATUSES = ("link_missing", "page_failed", "download_failed", "ambiguous", "rollback")
# How long a confirmed copy stays servable without a newer confirmation: a daily watcher
# plus a missed day and a retry. Past it, Phase C holds as 'stale'.
CONFIRMATION_TTL_HOURS = 36
# The county page each summons is linked from: the watcher reads it, and the PDF it links
# must come from the same HTTPS origin. Nothing else is ever fetched for a county.
SUMMONS_PAGES = {("douglas", "WA"): "https://www.douglascountywa.gov/163/Tax-Foreclosure-Information"}
MAX_SUMMONS_BYTES = 30_000_000  # the 2026 summons is 4.9 MB


class TaxReviewError(Exception):
    """A review step refused; nothing was written. Messages name rows by page and row,
    never by person, and never echo what was typed."""


def _county_key(county: str, state: str) -> tuple[str, str]:
    key = (county.strip().lower(), state.strip().upper())
    if key not in _PARCEL:
        raise TaxReviewError(f"{key[0]}, {key[1]} has no checked summons format")
    return key


def _lock_county(db, county: str, state: str) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:k))"),
               {"c": _LOCK_CLASS, "k": f"{county}/{state}"})


def _version_id(version_id) -> str:
    try:
        return str(uuid.UUID(str(version_id)))
    except ValueError as exc:
        raise TaxReviewError("no summons version with that id") from exc


def _lock_version(db, version_id) -> Version:
    """Take the version's county lock, then read the version fresh under it."""
    vid = _version_id(version_id)
    where = db.execute(select(Version.county, Version.state).where(Version.id == vid)).one_or_none()
    if where is None:
        raise TaxReviewError("no summons version with that id")
    _lock_county(db, where.county, where.state)
    # FOR UPDATE as well: a row insert that bypasses this module takes FOR SHARE on its
    # version (tax_summons_rows_seal), so it cannot slip in while a publish validates.
    return db.execute(select(Version).where(Version.id == vid).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one()


def _event(db, v: Version, action: str, actor: str, **kw) -> None:
    db.add(Event(version_id=v.id, revision=v.revision, action=action, actor=actor, **kw))


def _open_version(db, *, county: str, state: str, source_url: str, sha256: str) -> tuple[str, bool]:
    """The version for this digest, created pending if it is new. Returns (id, created).
    The caller holds the county lock and records the observation (record_observation)."""
    sha256 = sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise TaxReviewError("sha256 is not a hex digest")
    now = datetime.now(UTC)
    v = db.execute(select(Version).where(Version.county == county, Version.state == state,
                                         Version.sha256 == sha256)
                   .execution_options(populate_existing=True)).scalar_one_or_none()
    if v is not None:
        v.last_checked_at = now
        db.flush()
        return str(v.id), False
    # seen_order is assigned by the database (tax_summons_versions_seal), under this lock.
    v = Version(county=county, state=state, sha256=sha256, source_url=source_url,
                status="pending", revision=0, first_seen_at=now, last_checked_at=now)
    db.add(v)
    db.flush()
    return str(v.id), True


def _require_pending(v: Version, doing: str) -> None:
    if v.status != "pending":
        raise TaxReviewError(f"the version is {v.status}; only a pending version {doing}")


def _require_same_pdf(v: Version, pdf_sha256: str) -> None:
    """The operator works from a downloaded copy; it must be this version's bytes. The
    county URL can serve newer bytes than the version recorded."""
    if (pdf_sha256 or "").lower() != v.sha256:
        raise TaxReviewError("the PDF you have is not this version's copy (its digest differs); "
                             "the county may have replaced it: wait for the new copy to be recorded")


def _established_case(db, v: Version) -> str | None:
    """The case of the county's latest PUBLISHED copy (a pending copy's case, possibly a
    mistake, never becomes the rule for the next one)."""
    return db.execute(select(Version.case_number).where(
        Version.county == v.county, Version.state == v.state, Version.id != v.id,
        Version.published_at.is_not(None)).order_by(Version.published_seq.desc()).limit(1)).scalar()


def _observation(db, county: str, state: str) -> Observation:
    """The county's observation row, read fresh and locked (created, unobserved, on first
    use). The caller holds the county lock."""
    obs = db.execute(select(Observation).where(Observation.county == county, Observation.state == state)
                     .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if obs is None:
        obs = Observation(county=county, state=state, status="unknown", claim_token=0)
        db.add(obs)
        db.flush()
    return obs


def current_copy(db, version_id) -> tuple[str, str] | None:
    """(link_url, sha256) when this version is the copy the watcher last confirmed its
    county serves and the latest check found nothing wrong; else None. The URL is the one
    the watcher confirmed, which can be newer than the version's first-seen address."""
    vid = _version_id(version_id)
    row = db.execute(select(Observation.link_url, Version.sha256).join(
        Version, Version.id == Observation.current_version_id)
        .where(Observation.current_version_id == vid, Observation.status == "ok")).one_or_none()
    return None if row is None else (row[0], row[1])


def _require_current(db, v: Version) -> None:
    current = db.execute(select(Observation.current_version_id).where(
        Observation.county == v.county, Observation.state == v.state, Observation.status == "ok")).scalar()
    if current != v.id:
        raise TaxReviewError("this is not the copy the county serves now (the watcher has confirmed "
                             "another, or its latest check is a hold); review the current one")


def claim_watch(db, *, county: str, state: str, lease_seconds: int) -> int | None:
    """Claim the county for one watcher run, BEFORE anything is fetched. Returns the run's
    fencing token, or None while another run's lease is still live."""
    county, state = _county_key(county, state)
    _lock_county(db, county, state)
    obs = _observation(db, county, state)
    # The wall clock after the lock was won, not the transaction's start.
    live = db.execute(select(Observation.claim_expires_at > func.clock_timestamp()).where(
        Observation.county == county, Observation.state == state)).scalar()
    if live:
        return None
    obs.claim_token += 1
    obs.claim_expires_at = func.clock_timestamp() + text(f"interval '{int(lease_seconds)} seconds'")
    db.flush()
    return obs.claim_token


def record_observation(db, *, county: str, state: str, token: int, hold: str | None = None,
                       source_url: str | None = None, link_year: int | None = None,
                       sha256: str | None = None) -> dict:
    """Record one watcher run's result, if its claim is still the latest (fencing).

    A hold keeps the last confirmed copy for diagnosis but stops it being served. A link
    year lower than one already confirmed is a rollback hold, never a confirmation.
    Returns {"status", "version_id", "created", "published"}."""
    county, state = _county_key(county, state)
    _lock_county(db, county, state)
    obs = _observation(db, county, state)
    if obs.claim_token != token:
        raise TaxReviewError("a newer watcher run has claimed this county; this run's result is stale")
    live = db.execute(select(Observation.claim_expires_at > func.clock_timestamp()).where(
        Observation.county == county, Observation.state == state)).scalar()
    if token <= 0 or not live:  # never claimed, already recorded (a replay), or its lease ran out
        raise TaxReviewError("this run holds no live claim; its result is not recorded")
    if hold is None and obs.max_link_year is not None and link_year is not None and link_year < obs.max_link_year:
        hold = "rollback"
    # Every field of the result is set together and written once: the database takes a
    # result only as a whole (tax_summons_observations_guard).
    if hold is not None:
        if hold not in HOLD_STATUSES:
            raise TaxReviewError(f"unknown hold {hold!r}")
        obs.claim_expires_at, obs.checked_at, obs.status = None, func.now(), hold
        if hold == "rollback":
            obs.link_url, obs.link_year = source_url, link_year
        db.flush()
        return {"status": hold, "version_id": obs.current_version_id, "created": False, "published": False}
    if not (source_url and link_year and sha256):
        raise TaxReviewError("a confirmed copy needs its URL, link year and digest")
    vid, created = _open_version(db, county=county, state=state, source_url=source_url, sha256=sha256)
    obs.claim_expires_at, obs.checked_at = None, func.now()  # the claim is consumed with the result
    obs.status, obs.current_version_id, obs.confirmed_at = "ok", vid, func.now()
    obs.link_url, obs.link_year = source_url, link_year
    obs.max_link_year = max(link_year, obs.max_link_year or link_year)
    db.flush()
    status = db.execute(select(Version.status).where(Version.id == vid)).scalar()
    return {"status": "ok", "version_id": vid, "created": created, "published": status == "published"}


def serving_status(db, *, county: str, state: str) -> tuple[str, str | None]:
    """Whether the county's tax leads may be served now: ("ok", version_id) when the
    current copy is published and was confirmed within CONFIRMATION_TTL_HOURS of the
    database clock; otherwise (reason, version_id), the reason one of HOLD_STATUSES,
    "unobserved", "unpublished", "superseded" or "stale". Phase C asks this when it
    selects rows and again before it delivers."""
    county, state = _county_key(county, state)
    row = db.execute(
        select(Observation.status, Observation.current_version_id, Version.status,
               Observation.confirmed_at
               > func.statement_timestamp() - text(f"interval '{CONFIRMATION_TTL_HOURS} hours'"))
        .outerjoin(Version, Version.id == Observation.current_version_id)
        .where(Observation.county == county, Observation.state == state)).one_or_none()
    if row is None or row[0] == "unknown":
        return "unobserved", None
    obs_status, vid, version_status, fresh = row
    if obs_status != "ok":
        return obs_status, vid  # a hold, even before any copy was ever confirmed
    if version_status != "published":
        return ("superseded" if version_status == "superseded" else "unpublished"), vid
    if not fresh:
        return "stale", vid
    return "ok", vid


def set_manifest(db, version_id, *, case_number: str, rows_per_page: dict[int, int], actor: str,
                 pdf_sha256: str, amount_as_of: date, new_case: bool = False) -> int:
    """Record the operator's reading of the PDF. Any rows loaded before it no longer
    count: the manifest starts a new, empty revision. Returns that revision.

    A re-uploaded summons keeps the case it was filed under, so a different case number
    is refused unless ``new_case`` says this is a later year's summons. That stops a
    mistyped case number from hiding the parcels the county stamped redeemed."""
    v = _lock_version(db, version_id)
    _require_pending(v, "takes a manifest")
    _require_current(db, v)
    _require_same_pdf(v, pdf_sha256)
    case_number = (case_number or "").strip()
    if not _CASE.fullmatch(case_number):
        raise TaxReviewError("case number must look like 26-2-00337-09")
    # The date the PDF states its amounts as of; a summons never states a future date.
    if (not isinstance(amount_as_of, date) or isinstance(amount_as_of, datetime)
            or not date(2000, 1, 1) <= amount_as_of <= datetime.now(UTC).date()):
        raise TaxReviewError("the as-of date must be the summons' own (e.g. 2026-08-01), not in the future")
    established = _established_case(db, v)
    if new_case:
        if established is not None and int(case_number[:2]) <= int(established[:2]):
            raise TaxReviewError(f"a new case must be filed in a later year than {established}")
    elif established is not None and case_number != established:
        raise TaxReviewError(f"the county's summons is case {established}; for a later year's summons "
                             f"use --new-case")
    if (not isinstance(rows_per_page, dict) or not rows_per_page
            or not all(_positive_int(p) and p <= _MAX_PAGE and _positive_int(n) and n <= _MAX_ROWS_PER_PAGE
                       for p, n in rows_per_page.items())
            or sum(rows_per_page.values()) > _MAX_ROWS):
        raise TaxReviewError(f"pages must be page:rows pairs, pages 1-{_MAX_PAGE}, 1-{_MAX_ROWS_PER_PAGE} rows "
                             f"a page, at most {_MAX_ROWS} rows in all")
    v.case_number = case_number
    v.amount_as_of = amount_as_of
    v.manifest = {str(p): n for p, n in sorted(rows_per_page.items())}
    v.revision += 1
    _event(db, v, "manifest", actor, reason="new_case" if new_case and case_number != established else None)
    db.flush()
    return v.revision


def _expected_locators(v: Version) -> set[tuple[int, int]]:
    return {(int(p), r) for p, n in (v.manifest or {}).items() for r in range(1, n + 1)}


def _current_rows(db, v: Version, revision: int | None = None) -> list[Row]:
    rev = v.revision if revision is None else revision
    return db.execute(select(Row).where(Row.version_id == v.id, Row.revision == rev)
                      .order_by(Row.page, Row.row_no)).scalars().all()


def _prior_published(db, v: Version) -> Version | None:
    """The case's last published copy."""
    return db.execute(select(Version).where(
        Version.county == v.county, Version.state == v.state, Version.id != v.id,
        Version.case_number == v.case_number, Version.published_at.is_not(None))
        .order_by(Version.published_seq.desc()).limit(1)).scalar_one_or_none()


def draft_rows(db, version_id) -> dict:
    """A file the operator edits: the case's last published rows, all unconfirmed."""
    vid = _version_id(version_id)
    v = db.get(Version, vid)
    if v is None:
        raise TaxReviewError("no summons version with that id")
    if not v.manifest:
        raise TaxReviewError("the version has no manifest; record one first")
    if v.amount_as_of is None:
        raise TaxReviewError("the version has no as-of date; record the manifest again")
    prior = _prior_published(db, v)
    rows = [] if prior is None else _current_rows(db, prior, prior.published_revision)
    return {"version": str(v.id), "case": v.case_number, "as_of": v.amount_as_of.isoformat(),
            "manifest": v.manifest, "rows": [{
        "page": r.page, "row": r.row_no, "parcel": r.parcel_id, "vested_owner": r.vested_owner,
        "taxpayer": r.taxpayer, "property_address": r.property_address,
        "mailing_address": r.mailing_address,
        "amount": None if r.amount_due is None else str(r.amount_due),
        "tax_years": r.tax_years, "eligibility": r.eligibility,
        **({"reason": r.exclusion_reason} if r.exclusion_reason else {}),
        "confirmed": False,
    } for r in rows]}


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _amount(value, where: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TaxReviewError(f"{where}: amount is not a number")
    raw = str(value).strip()
    # $1,234.56 / 1234.56 / 1234: grouping, if any, must be thousands; never guess.
    if isinstance(value, float) or not _AMOUNT.fullmatch(raw):
        raise TaxReviewError(f"{where}: amount must be dollars and cents like 1,234.56")
    try:
        amt = Decimal(raw.replace(",", "").replace("$", ""))
    except InvalidOperation as exc:
        raise TaxReviewError(f"{where}: amount is not a number") from exc
    if not amt.is_finite() or amt <= 0 or amt > _MAX_AMOUNT or amt != amt.quantize(Decimal("0.01")):
        raise TaxReviewError(f"{where}: amount must be positive dollars and cents")
    return amt


def _tax_years(value, where: str) -> list[int] | None:
    if value is None:
        return None
    this_year = datetime.now(UTC).year
    if (not isinstance(value, list) or not value
            or not all(_positive_int(y) and 1990 <= y <= this_year for y in value)):
        raise TaxReviewError(f"{where}: tax_years must be a list of years")
    return sorted(set(value))


def _text(value, where: str, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise TaxReviewError(f"{where}: {key} must be text up to 512 characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TaxReviewError(f"{where}: {key} is not valid text") from exc
    # Control and format characters (NUL, terminal escapes, bidi overrides) never belong
    # in a name or address, and could disguise what `show` puts in front of the operator.
    # Plain whitespace (tab, newline) is folded to single spaces below instead.
    if any(unicodedata.category(ch)[0] == "C" for ch in value if ch not in "\t\n\r"):
        raise TaxReviewError(f"{where}: {key} holds a control character")
    return " ".join(value.split()) or None


def _parse_row(raw, i: int, parcel_shape: re.Pattern) -> dict:
    if not isinstance(raw, dict):
        raise TaxReviewError(f"row #{i + 1} is not an object")
    page, row_no = raw.get("page"), raw.get("row")
    if not (_positive_int(page) and _positive_int(row_no)):
        raise TaxReviewError(f"row #{i + 1}: page and row must be positive whole numbers")
    where = f"page {page} row {row_no}"
    unknown = set(raw) - _ROW_KEYS
    if unknown:  # never echoed: a misplaced name could be a key
        raise TaxReviewError(f"{where}: {len(unknown)} unknown field(s)")
    if raw.get("confirmed") is not True:
        raise TaxReviewError(f"{where}: not confirmed against the PDF (set \"confirmed\": true)")
    eligibility = raw.get("eligibility")
    if eligibility not in ELIGIBILITY:
        raise TaxReviewError(f"{where}: eligibility must be one of {', '.join(ELIGIBILITY)}")
    parcel = raw.get("parcel")
    if parcel is not None:
        parcel = re.sub(r"[\s-]", "", parcel) if isinstance(parcel, str) else parcel
        if not isinstance(parcel, str) or not parcel.isascii() or not parcel_shape.fullmatch(parcel):
            raise TaxReviewError(f"{where}: parcel is not in this county's parcel format")
    row = {"page": page, "row_no": row_no, "parcel_id": parcel, "eligibility": eligibility,
           "amount_due": _amount(raw.get("amount"), where),
           "tax_years": _tax_years(raw.get("tax_years"), where),
           **{k: _text(raw.get(k), where, k) for k in _TEXT_KEYS}}
    reason = raw.get("reason")
    if eligibility == "excluded":
        if reason not in EXCLUDE_REASONS:
            raise TaxReviewError(f"{where}: an excluded row needs a reason: {', '.join(EXCLUDE_REASONS)}")
    elif reason is not None:
        raise TaxReviewError(f"{where}: only an excluded row takes a reason")
    row["exclusion_reason"] = reason
    reinstate = raw.get("reinstate_reason")
    if reinstate is not None and (eligibility != "eligible" or reinstate not in REINSTATE_REASONS):
        raise TaxReviewError(f"{where}: reinstate_reason is for an eligible row: "
                             f"{', '.join(REINSTATE_REASONS)}")
    row["reinstate_reason"] = reinstate
    if eligibility == "eligible":
        missing = [k for k, f in (("parcel", "parcel_id"), ("amount", "amount_due"),
                                  ("tax_years", "tax_years"), ("vested_owner", "vested_owner")) if not row[f]]
        if missing:
            raise TaxReviewError(f"{where}: an eligible row needs {', '.join(missing)}")
    if eligibility == "redeemed" and parcel is None:
        raise TaxReviewError(f"{where}: a redeemed row needs its parcel; if it cannot be read, "
                             f"mark the row unreadable")
    return row


def _blocked_parcels(db, v: Version) -> set[str]:
    """Parcels whose latest published decision in this case is 'redeemed'. Latest in
    publication order (published_seq, set by the database), never by a timestamp."""
    decisions = db.execute(
        select(Row.parcel_id, Row.eligibility)
        .join(Version, (Version.id == Row.version_id) & (Version.published_revision == Row.revision))
        .where(Version.county == v.county, Version.state == v.state, Version.case_number == v.case_number,
               Version.published_at.is_not(None), Version.id != v.id,
               Row.eligibility.in_(("redeemed", "eligible")), Row.parcel_id.is_not(None))
        .order_by(Version.published_seq)).all()
    latest: dict[str, str] = {}
    for parcel, eligibility in decisions:
        latest[parcel] = eligibility
    return {p for p, e in latest.items() if e == "redeemed"}


def _check_redemptions(db, v: Version, rows: list[dict]) -> None:
    blocked = _blocked_parcels(db, v)
    for r in rows:
        where = f"page {r['page']} row {r['row_no']}"
        if r["eligibility"] == "eligible" and r["parcel_id"] in blocked and not r["reinstate_reason"]:
            raise TaxReviewError(f"{where}: the county stamped this parcel redeemed earlier in the case; "
                                 f"reinstate it with reinstate_reason")
        if r["reinstate_reason"] and r["parcel_id"] not in blocked:
            raise TaxReviewError(f"{where}: nothing to reinstate")


def _check_complete(v: Version, locators: list[tuple[int, int]]) -> None:
    expected = _expected_locators(v)
    if not expected:
        raise TaxReviewError("the version has no manifest; record one first")
    got = set(locators)
    if len(got) != len(locators):
        raise TaxReviewError("a page and row appear twice")
    missing, extra = sorted(expected - got), sorted(got - expected)
    if missing or extra:
        def fmt(locs):
            return ", ".join(f"p{p} r{r}" for p, r in locs[:10]) or "none"
        raise TaxReviewError(f"rows do not match the manifest: missing {fmt(missing)}; not in it {fmt(extra)}")


def load_rows(db, version_id, payload, *, actor: str) -> tuple[int, dict[str, int]]:
    """Store the operator's confirmed rows as the version's new revision. All or nothing.
    Returns (revision, counts by eligibility)."""
    v = _lock_version(db, version_id)
    _require_pending(v, "takes rows")
    if not v.manifest:
        raise TaxReviewError("the version has no manifest; record one first")
    if v.amount_as_of is None:
        raise TaxReviewError("the version has no as-of date; record the manifest again")
    if not isinstance(payload, dict) or set(payload) != {"version", "case", "as_of", "manifest", "rows"}:
        raise TaxReviewError("the file must hold exactly the version, case, as-of date, manifest and rows "
                             "a draft has")
    if payload["version"] != str(v.id):
        raise TaxReviewError("the file was drafted for a different version")
    if (payload["case"] != v.case_number or payload["as_of"] != v.amount_as_of.isoformat()
            or payload["manifest"] != v.manifest):
        raise TaxReviewError("the file was drafted under a different case, as-of date or manifest; draft it again")
    raws = payload["rows"]
    if not isinstance(raws, list) or not raws:
        raise TaxReviewError("the file holds no rows")
    shape = _PARCEL[_county_key(v.county, v.state)]
    rows = [_parse_row(raw, i, shape) for i, raw in enumerate(raws)]
    _check_complete(v, [(r["page"], r["row_no"]) for r in rows])
    seen_parcel = set()
    for r in rows:
        if r["parcel_id"] is not None:
            if r["parcel_id"] in seen_parcel:
                raise TaxReviewError(f"page {r['page']} row {r['row_no']}: parcel appears on two rows")
            seen_parcel.add(r["parcel_id"])
    _check_redemptions(db, v, rows)

    v.revision += 1
    db.flush()  # the new revision first: the database only takes rows into the current one
    _event(db, v, "loaded", actor)
    for r in rows:
        db.add(Row(version_id=v.id, revision=v.revision, entered_by=actor, **r))
        _event(db, v, "entered", actor, parcel_id=r["parcel_id"], eligibility=r["eligibility"],
               reason=r["exclusion_reason"])
        if r["reinstate_reason"]:
            _event(db, v, "reinstated", actor, parcel_id=r["parcel_id"], eligibility="eligible",
                   reason=r["reinstate_reason"])
    db.flush()
    return v.revision, counts_by_eligibility(db, v.id)


def publish(db, version_id, *, actor: str, expected_revision: int, pdf_sha256: str) -> str | None:
    """Approve revision ``expected_revision`` of a pending version, checked against the
    operator's copy of its PDF: it becomes the county's published version and the previous
    one is superseded (so is every other pending copy). Returns the superseded published
    version's id, if any."""
    v = _lock_version(db, version_id)
    _require_pending(v, "can be published")
    if v.amount_as_of is None:  # before anything changes (the database refuses it too)
        raise TaxReviewError("the version has no as-of date; record the manifest again")
    _require_same_pdf(v, pdf_sha256)
    if v.revision != expected_revision:
        raise TaxReviewError(f"the version is at revision {v.revision}, not {expected_revision}: "
                             f"its rows changed since you looked; show it again")
    rows = _current_rows(db, v)
    if not rows:
        raise TaxReviewError("the version has no rows at this revision; load them first")
    _check_complete(v, [(r.page, r.row_no) for r in rows])
    shape = _PARCEL[_county_key(v.county, v.state)]
    for r in rows:  # rows can reach the table past load_rows; the format still binds
        if r.parcel_id is not None and not shape.fullmatch(r.parcel_id):
            raise TaxReviewError(f"page {r.page} row {r.row_no}: parcel is not in this county's parcel format")
    _require_current(db, v)
    county_versions = db.execute(select(Version).where(Version.county == v.county, Version.state == v.state)
                                 .execution_options(populate_existing=True)).scalars().all()
    _check_redemptions(db, v, [{"page": r.page, "row_no": r.row_no, "parcel_id": r.parcel_id,
                                "eligibility": r.eligibility, "reinstate_reason": r.reinstate_reason}
                               for r in rows])
    superseded = None
    for o in county_versions:
        if o.id != v.id and o.status in ("published", "pending"):
            if o.status == "published":
                superseded = str(o.id)
            o.status = "superseded"
    db.flush()  # the one-published index sees the old one go before the new one arrives
    v.status, v.published_by, v.published_at = "published", actor, datetime.now(UTC)
    v.published_revision = v.revision
    _event(db, v, "published", actor)
    db.flush()
    return superseded


def counts_by_eligibility(db, version_id) -> dict[str, int]:
    v = db.get(Version, _version_id(version_id))
    counts = dict.fromkeys(ELIGIBILITY, 0)
    if v is not None:
        for r in _current_rows(db, v):
            counts[r.eligibility] += 1
    return counts


__all__ = ["CONFIRMATION_TTL_HOURS", "ELIGIBILITY", "EXCLUDE_REASONS", "HOLD_STATUSES", "REINSTATE_REASONS",
           "MAX_SUMMONS_BYTES", "SUMMONS_PAGES", "TaxReviewError", "claim_watch", "counts_by_eligibility",
           "current_copy", "draft_rows", "load_rows", "publish", "record_observation", "serving_status",
           "set_manifest"]
