"""Name the owner of a Tacoma code-violation parcel from the Pierce taxpayer record.

WHY
---
Tacoma's code-violation layer names the case, never the owner, so every Pierce
code_violation lead reached customers with a case label ("Nuisance - 1603 N ALDER ST")
where the owner belongs, and skip trace could never run for it. The Tacoma record
already carries the real Pierce parcel number (set at scrape, never inferred), and the
Pierce Assessor-Treasurer Information Portal (ATIP) is the only authoritative public
source of the current taxpayer for a parcel (the county withholds names from its bulk
Data Mart; no GIS layer carries them).

COMPLIANCE (owner decision 2026-09-14, recorded here on purpose)
----------------------------------------------------------------
The ATIP footer cites RCW 42.56.070(8) (no commercial use of LISTS OF INDIVIDUALS).
Legal review cleared storing the ATIP taxpayer name FOR CODE-VIOLATION OWNER NAMING
ONLY. Every other Pierce product stays address-only: `pierce_atip.parse_summary` still
refuses to emit a name, and this module names a row only when its
enrichment_data.source is the Tacoma code-violation source. The gate is
`settings.PIERCE_CV_OWNER_ENABLED` (default off).

HOW
---
One normal headless browser page view per parcel: the ATIP property page loads and the
portal's own code calls `/api/pcAtipSummary` with its own invisible reCAPTCHA. We read
that response. No captcha solver, no direct token handling, and never any key the
portal exposes. The response classes and the stop rules are pierce_atip's (an empty
body is a rejected verification; restart the session ONCE, then stop; three hard
failures in a row stop the batch and put the source in cooldown).

A name is accepted only when ALL hold (anything else stores a terminal status and names
nobody):
  * the row echoes the parcel we asked for;
  * the account is Real Property (not Personal Property, not a mobile home);
  * it is not a reference record (a condominium master parcel, whose "taxpayer" is the
    placeholder REFERENCE, not the unit owner);
  * the ATIP situs is the lead's address after Tacoma normalization: Tacoma's layer
    drops the word TACOMA from street names ("2117 AVE S" is 2117 TACOMA AVE S), the
    county writes multi-address parcels as ranges ("602 TO 610 TACOMA AVE S") and
    appends unit lists ("5015 E 8TH ST UNIT A & B").
  * a taxpayer name is present. care_of is never a name.

Pacing is at least 2 s between page views, under a Redis lease that admits one Pierce
owner pass at a time and FAILS CLOSED (a Redis outage runs no lookups rather than
parallel browsers against the portal), behind the shared source-health cooldown. Every
lookup logs one audit line: parcel and outcome, never the name.

SSRF: the host is registered via add_scrape_domain, the parcel is validated as exactly
10 digits before it is placed in the path, and BridgeScraper validates every navigation.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from src.config import settings
from src.scrapers.enrichment.pierce_atip import (
    ATIP_HOST,
    ATIP_SUMMARY_API,
    FOUND,
    HARD_FAILURE,
    NOT_FOUND,
    TOKEN_REJECTED,
    classify_response,
)
from src.utils.address_intel import _normalize_street
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.pierce_atip_owner")

OWNER_SOURCE = "pierce_atip"
TACOMA_CV_SOURCE = "tacoma_code_violations"

# Terminal per-row outcomes (enrichment_data.owner_status). A row carrying any of them
# is never looked up again by the live pass or the sweep.
MATCHED = "matched"
PARCEL_MISMATCH = "parcel_mismatch"
NOT_REAL_PROPERTY = "not_real_property"
REFERENCE_PARCEL = "reference_parcel"
ADDRESS_MISMATCH = "address_mismatch"
NOT_ON_RECORD = "not_on_record"
NO_NAME = "no_name"
GAVE_UP = "gave_up"

MIN_PACE_S = 2.0
_PAGE_TIMEOUT_S = 45.0
_SESSION_START_S = 30.0
_TIME_LIMITS = ("SoftTimeLimitExceeded", "TimeLimitExceeded")
_MAX_HARD_FAILURES = 3
_LEASE_WAIT_S = 30.0
_NAME_MAX = 512

_PARCEL_RE = re.compile(r"^\d{10}$")
_REAL_PROPERTY = "REAL PROPERTY"
_REFERENCE = "REFERENCE"
# "633 TO 649 DIVISION AVE": a parcel carrying a range of house numbers.
_RANGE_RE = re.compile(r"^(\d+)\s+TO\s+(\d+)\s+(.+)$")
_NUMBER_RE = re.compile(r"^(\d+)\s+(.+)$")
# Unit lists the county appends to a situs ("UNIT A - D", "UNIT ABCDEF", "#151"). The
# shared normalizer only strips a 1-2 character unit id, so the long forms are cut
# here. The parcel itself is the identity (echo check); the situs only has to be the
# same building address.
_UNIT_TAIL_RE = re.compile(r"\s+(?:#|UNIT\b|APT\b|STE\b|SUITE\b|SPC\b|SPACE\b).*$", re.I)
_TACOMA = "TACOMA"


def normalize_parcel(value: object) -> str | None:
    """The 10-digit Pierce parcel, or None. Leading zeros are significant; never int-cast."""
    pid = str(value or "").strip()
    return pid if _PARCEL_RE.match(pid) else None


def _street(address: str | None) -> str:
    # The street is everything before the first comma. Not the display parser: it
    # reads a trailing TACOMA as the city, and "1717 SOUTH TACOMA WAY" is a street.
    street = (address or "").split(",")[0].strip()
    if not street:
        return ""
    return _normalize_street(_UNIT_TAIL_RE.sub("", street.upper()))


def _split_number(street: str) -> tuple[int, int, str] | None:
    """(low, high, rest) for "N rest" or "LOW TO HIGH rest"; None without a house number."""
    m = _RANGE_RE.match(street)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (min(lo, hi), max(lo, hi), m.group(3))
    m = _NUMBER_RE.match(street)
    if m:
        n = int(m.group(1))
        return (n, n, m.group(2))
    return None


def _same_street_name(lead_rest: str, atip_rest: str) -> bool:
    if lead_rest == atip_rest:
        return True
    # Tacoma's layer drops TACOMA from street names ("AVE S" for TACOMA AVE S, "S WAY"
    # for SOUTH TACOMA WAY). Only that one word may be missing, only on the lead side.
    lead_tokens = lead_rest.split(" ")
    if _TACOMA in lead_tokens:
        return False
    stripped = [t for t in atip_rest.split(" ") if t != _TACOMA]
    return len(stripped) < len(atip_rest.split(" ")) and stripped == lead_tokens


def situs_agrees(lead_address: str | None, atip_situs: str | None) -> bool:
    """Is the ATIP situs the lead's address, after Tacoma normalization?"""
    lead, atip = _street(lead_address), _street(atip_situs)
    if not lead or not atip:
        return False
    lead_parts, atip_parts = _split_number(lead), _split_number(atip)
    if lead_parts is None or atip_parts is None:
        return False
    lead_lo, lead_hi, lead_rest = lead_parts
    lo, hi, atip_rest = atip_parts
    if lead_lo != lead_hi:
        # A lead range must be the same range, not merely overlap it.
        if (lead_lo, lead_hi) != (lo, hi):
            return False
    elif lo == hi:
        if lead_lo != lo:
            return False
    else:
        # A range addresses one side of one block: same parity as both ends.
        if not (lo <= lead_lo <= hi) or (lo % 2 == hi % 2 and lead_lo % 2 != lo % 2):
            return False
    return _same_street_name(lead_rest, atip_rest)


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


@dataclass(frozen=True)
class OwnerDecision:
    status: str
    name: str | None = None


def decide(parcel: str, rows: list[dict] | None, lead_address: str | None, *,
           source: object) -> OwnerDecision:
    """Apply every acceptance rule to one ATIP answer for one lead.

    `rows` is the classified summary body: [] (or None) when the parcel is not on
    record. The name leaves this function only inside a MATCHED decision. `source` is
    the lead's enrichment_data.source: the 2026-09-14 clearance covers Tacoma code
    violations only, so any other lead is refused here, not just by the callers.
    """
    if source != TACOMA_CV_SOURCE:
        raise ValueError("ATIP taxpayer names are cleared for Tacoma code-violation leads only")
    if not rows:
        return OwnerDecision(NOT_ON_RECORD)
    echoed = [r for r in rows if _clean(r.get("parcel_number")) == parcel]
    if len(echoed) != 1:
        # Nothing for the parcel we asked for, or an ambiguous answer: never guess.
        return OwnerDecision(PARCEL_MISMATCH)
    row = echoed[0]
    if _clean(row.get("acct_type")).upper() != _REAL_PROPERTY:
        return OwnerDecision(NOT_REAL_PROPERTY)
    name = _clean(row.get("name"))
    situs = _clean(row.get("situs"))
    use_code = _clean(row.get("use_cd")).upper()
    if _REFERENCE in (situs.upper(), name.upper()) or use_code.startswith("0000"):
        return OwnerDecision(REFERENCE_PARCEL)
    if not situs_agrees(lead_address, situs):
        return OwnerDecision(ADDRESS_MISMATCH)
    if not name:
        return OwnerDecision(NO_NAME)
    return OwnerDecision(MATCHED, name[:_NAME_MAX])


# ── Fetch: one real page view per parcel ─────────────────────────────────────────

_SUMMARY = urlparse(ATIP_SUMMARY_API)


def _summary_is_for(url: str, parcel: str) -> bool:
    """Exactly https://atip.piercecountywa.gov/api/pcAtipSummary?iParcelNumber=<parcel>."""
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return False
    return (u.scheme == "https" and u.hostname == _SUMMARY.hostname and port is None
            and u.path == _SUMMARY.path and not u.username and not u.password
            and parse_qs(u.query) == {"iParcelNumber": [parcel]})


def _new_session():
    """A normal headless browser session (the project's SSRF-guarded BridgeScraper)."""
    from src.scrapers.base_scraper import BridgeScraper

    return BridgeScraper()


async def _fetch_summary(session, parcel: str) -> tuple[int, str]:
    """(HTTP status, body) of the summary call the ATIP page itself makes for `parcel`."""
    page_url = f"https://{ATIP_HOST}/app/v2/propertyDetail/{parcel}/summary"
    async with session.page.expect_response(
        lambda resp: _summary_is_for(resp.url, parcel), timeout=_PAGE_TIMEOUT_S * 1000,
    ) as info:
        await session.safe_goto(page_url, timeout_ms=int(_PAGE_TIMEOUT_S * 1000))
    resp = await info.value
    return resp.status, await resp.text()


@dataclass
class Fetched:
    kind: str                 # FOUND | NOT_FOUND
    rows: list[dict] | None


def _new_stats() -> dict:
    return {"outcome": None, "requested": 0, "attempted": [], "found": [], "not_on_record": [],
            "transient": [], "token_rejected": 0, "sessions": 0}


def _audit(parcel: str, outcome: str) -> None:
    # Parcel and outcome only: the name is stored on the lead, never logged.
    _logger.info("pierce_atip_owner lookup parcel=%s outcome=%s", parcel, outcome)


async def _lookup(parcels: list[str], *, pace_s: float, deadline: float | None,
                  admission, stats: dict, out: dict[str, Fetched]) -> None:
    from src.scrapers.enrichment.source_health import PIERCE_ATIP_OWNER, record_source_blocked

    session = None
    restarted = False
    consecutive_hard = 0

    def _affordable(*, new_session: bool, pace: bool) -> bool:
        # Everything the next page view may cost must fit: the pause, a browser start
        # when one is needed, and the full page timeout (Codex r1 P2).
        if deadline is None:
            return True
        cost = _PAGE_TIMEOUT_S + (pace_s if pace else 0.0) + (_SESSION_START_S if new_session else 0.0)
        return time.monotonic() + cost < deadline

    async def _one(pid: str) -> tuple[str, list | None]:
        try:
            status, body = await asyncio.wait_for(_fetch_summary(session, pid),
                                                  timeout=_PAGE_TIMEOUT_S + 5)
            return classify_response(status, body)
        except BaseException as exc:
            if type(exc).__name__ in _TIME_LIMITS:
                _audit(pid, "time_limit")  # the task is being killed mid-lookup
                raise
            if not isinstance(exc, Exception):
                raise
            _logger.warning("pierce_atip_owner page failed for parcel %s: %s",
                            pid, type(exc).__name__)
            return HARD_FAILURE, None

    try:
        for i, pid in enumerate(parcels):
            if not _affordable(new_session=session is None, pace=bool(i)):
                stats["outcome"] = "budget_exhausted"
                return
            if not admission.still_held():
                stats["outcome"] = "lease_lost"
                return
            if session is None:
                session = await _new_session().__aenter__()
                stats["sessions"] += 1
            if i:
                await asyncio.sleep(pace_s)
            stats["attempted"].append(pid)
            kind, rows = await _one(pid)
            if kind == TOKEN_REJECTED and not restarted:
                # The portal declined this session's verification. One fresh session
                # for the whole batch (pierce_atip's re-solve-once rule), then stop.
                stats["token_rejected"] += 1
                restarted = True
                if not _affordable(new_session=True, pace=True):
                    stats["transient"].append(pid)
                    _audit(pid, "verification_rejected")
                    stats["outcome"] = "budget_exhausted"
                    return
                await session.__aexit__(None, None, None)
                session = None
                session = await _new_session().__aenter__()
                stats["sessions"] += 1
                await asyncio.sleep(pace_s)
                kind, rows = await _one(pid)
            if kind == TOKEN_REJECTED:
                stats["token_rejected"] += 1
                stats["transient"].append(pid)
                _audit(pid, "verification_rejected")
                record_source_blocked(PIERCE_ATIP_OWNER, "ATIP rejected verification twice")
                stats["outcome"] = "verification_rejected"
                return
            if kind == HARD_FAILURE:
                stats["transient"].append(pid)
                _audit(pid, "hard_failure")
                consecutive_hard += 1
                if consecutive_hard >= _MAX_HARD_FAILURES:
                    record_source_blocked(
                        PIERCE_ATIP_OWNER, f"{consecutive_hard} consecutive ATIP failures")
                    stats["outcome"] = "source_failing"
                    return
                continue
            consecutive_hard = 0
            if kind == NOT_FOUND:
                out[pid] = Fetched(NOT_FOUND, None)
                stats["not_on_record"].append(pid)
                _audit(pid, "not_on_record")
            elif kind == FOUND:
                out[pid] = Fetched(FOUND, rows)
                stats["found"].append(pid)
                _audit(pid, "found")
        stats["outcome"] = "complete"
    finally:
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001 -- closing must not mask the result
                _logger.warning("pierce_atip_owner session close failed: %s", type(exc).__name__)


def lookup_parcels(parcel_ids: list[str], *, pace_s: float = MIN_PACE_S,
                   budget_s: float | None = None, stats: dict | None = None
                   ) -> dict[str, Fetched]:
    """ATIP answers for Tacoma code-violation parcels, keyed by the 10-digit parcel.

    Parcels that failed or were not reached are simply absent; `stats` (filled in place,
    so a caller whose timeout fires still sees it) says which were attempted. Never
    raises for a source problem. Makes ZERO requests when PIERCE_CV_OWNER_ENABLED is off,
    another pass holds the lease (or Redis cannot confirm it), or the source is in
    cooldown.
    """
    from src.scrapers.enrichment.source_admission import SourceAdmission
    from src.scrapers.enrichment.source_health import (
        PIERCE_ATIP_OWNER,
        SourceUnavailableError,
        check_source_or_raise,
    )

    st = stats if stats is not None else {}
    st.update(_new_stats())
    out: dict[str, Fetched] = {}
    if not settings.PIERCE_CV_OWNER_ENABLED:
        st["outcome"] = "disabled"
        return out
    wanted = list(dict.fromkeys(p for p in (normalize_parcel(x) for x in parcel_ids) if p))
    st["requested"] = len(wanted)
    if not wanted:
        st["outcome"] = "complete"
        return out
    start = time.monotonic()
    deadline = start + budget_s if budget_s is not None else None
    with SourceAdmission(PIERCE_ATIP_OWNER, max_wait_s=_LEASE_WAIT_S) as admission:
        if not admission.holds_lease:
            # Fail CLOSED: an unconfirmed lease (Redis down, or another pass on the
            # portal) must not become a second browser stream against the county.
            st["outcome"] = "not_admitted"
            _logger.info("pierce_atip_owner: lease not held; %d parcel(s) left for later",
                         len(wanted))
            return out
        try:
            check_source_or_raise(PIERCE_ATIP_OWNER)
        except SourceUnavailableError:
            st["outcome"] = "source_unavailable"
            return out
        pace = max(MIN_PACE_S, float(pace_s))
        try:
            asyncio.run(_lookup(wanted, pace_s=pace, deadline=deadline, admission=admission,
                                stats=st, out=out))
        except Exception as exc:  # noqa: BLE001 -- best-effort enrichment
            if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                raise
            st["outcome"] = st["outcome"] or f"error:{type(exc).__name__}"
            _logger.warning("pierce_atip_owner lookup stopped: %s", type(exc).__name__)
    _logger.info("pierce_atip_owner: %s", json.dumps(
        {k: (len(v) if isinstance(v, list) else v) for k, v in st.items()}))
    return out


# ── Rows ─────────────────────────────────────────────────────────────────────────

def is_tacoma_code_violation(res) -> bool:
    ed = getattr(res, "enrichment_data", None)
    return isinstance(ed, dict) and ed.get("source") == TACOMA_CV_SOURCE


def owner_lookup_parcels(rows) -> dict[str, list]:
    """{parcel: [rows]} for Tacoma code-violation rows that still need an owner.

    Only the Tacoma code-violation source may be named from ATIP (the 2026-09-14
    decision is scoped to it); a row with a party, an owner source or a terminal
    owner_status is never offered again.
    """
    out: dict[str, list] = {}
    for res in rows:
        if not is_tacoma_code_violation(res) or (res.party_name or "").strip():
            continue
        ed = res.enrichment_data
        if ed.get("owner_source") or ed.get("owner_status"):
            continue
        pid = normalize_parcel(res.parcel_id)
        if pid:
            out.setdefault(pid, []).append(res)
    return out


def owner_payload(parcel: str, decision: OwnerDecision, checked_at: str) -> dict:
    """The enrichment_data keys a decision writes (the name goes to party_name only)."""
    payload = {"owner_status": decision.status, "owner_checked_at": checked_at}
    if decision.status == MATCHED:
        payload.update({"owner_source": OWNER_SOURCE, "owner_pin": parcel})
    return payload


def _still_needs_owner(res, pid: str) -> bool:
    return (is_tacoma_code_violation(res) and not (res.party_name or "").strip()
            and normalize_parcel(res.parcel_id) == pid
            and not res.enrichment_data.get("owner_source")
            and not res.enrichment_data.get("owner_status"))


def plan_owner_decisions(pin_map: dict[str, list], fetched: dict[str, Fetched]
                         ) -> tuple[list[tuple], Counter]:
    """([(row, parcel, decision)], counts) for every reached row that still needs an owner.

    Rows whose parcel was not answered get nothing (a later pass retries them); a row
    that no longer qualifies is counted `stale` and left alone.
    """
    plans: list[tuple] = []
    counts: Counter = Counter()
    for pid, rows in pin_map.items():
        f = fetched.get(pid)
        if f is None:
            counts["unreached"] += len(rows)
            continue
        for res in rows:
            if not _still_needs_owner(res, pid):
                counts["stale"] += 1
                continue
            plans.append((res, pid, decide(pid, f.rows, res.property_address,
                                           source=res.enrichment_data.get("source"))))
    return plans, counts


# Every owner write re-proves, in the UPDATE itself, what the decision was made on: the
# same Tacoma code-violation row, parcel and address, still unnamed and undecided. A
# row changed by anything else while the portal was being asked is left alone
# (Codex r1 P1). Callers add their own scope (job status, delivery) on top.
OWNER_ROW_GUARD = """
      r.id = :rid AND r.user_id = :uid
  AND btrim(r.parcel_id) = :pid
  AND r.property_address IS NOT DISTINCT FROM CAST(:address AS varchar)
  AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
  AND r.enrichment_data::jsonb->>'source' = 'tacoma_code_violations'
  AND (r.party_name IS NULL OR btrim(r.party_name) = '')
  AND NOT (r.enrichment_data::jsonb ? 'owner_status')
  AND NOT (r.enrichment_data::jsonb ? 'owner_source')
"""

_WRITE_DECISION_SQL = f"""
    UPDATE results r SET
      party_name = COALESCE(CAST(:owner AS varchar), r.party_name),
      enrichment_data = (r.enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE {OWNER_ROW_GUARD}
"""  # noqa: S608 -- splices only the OWNER_ROW_GUARD constant; every value is bound


def write_owner_decisions(db, plans: list[tuple], *, checked_at: str) -> Counter:
    """Guarded UPDATE per planned row (live job pass); commits; returns status counts.

    The ORM objects are never mutated: each is expired after the commit so later
    enrichment steps read what the database actually holds.
    """
    from sqlalchemy import text as sa_text

    counts: Counter = Counter()
    for res, pid, d in plans:
        result = db.execute(sa_text(_WRITE_DECISION_SQL), {
            "rid": res.id, "uid": res.user_id, "pid": pid, "address": res.property_address,
            "owner": d.name if d.status == MATCHED else None,
            "payload": json.dumps(owner_payload(pid, d, checked_at)),
        })
        key = d.status if result.rowcount else "stale"
        counts[key] += 1
        _logger.info("pierce_atip_owner decision parcel=%s status=%s", pid, key)
    db.commit()
    for res, _pid, _d in plans:
        db.expire(res)
    return counts
