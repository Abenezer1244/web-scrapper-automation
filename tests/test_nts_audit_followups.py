"""Follow-ups from the 2026-09-02 Pierce auction-leads audit (items 5, 6, 7, 10):

* item 10 — a trustee's page TITLE carries a trailing dash on the TS# ("TS# WA-26-
  1035144-SW- NOTICE OF…", REAL notice saved as nts_tacoma_title_trailing_dash.txt)
  while the body is clean; the natural key must be the clean spelling, and the
  upsert retires the dashed twin cached by the old parser.
* item 5 — the crawl re-sweeps active, future-dated notices that still carry no
  amount (real DB, real parser, injected fetch so no network).
* item 6 — finalize warns/alerts on null default_amount (pure wording helper).
* item 7 — the repair script's amount pass can opt into pre_foreclosure rows.
"""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from src.db.models import NtsNotice
from src.db.session import system_sync_session
from src.scrapers.sources import nts_tacoma_index as nts
from src.workers import nts_crawler
from src.workers.trustee_sale_finalize import null_amount_alert

_FIXTURES = Path(__file__).parent / "fixtures"
_TS_PREFIX = "TEST-AUDIT-FOLLOWUP-"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class TestTitleTrailingDash:
    def test_ts_number_is_the_clean_spelling(self):
        p = nts.parse_nts_notice(_load("nts_tacoma_title_trailing_dash.txt"))
        assert p["ts_number"] == "WA-26-1035144-SW"
        assert p["principal_owing"] == Decimal("494742.64")
        assert p["auction_date"] == "8/14/2026"

    def test_all_dash_ts_becomes_none(self):
        assert nts.parse_nts_notice("Trustee Sale No.: --- Grantor(s): X")["ts_number"] is None


class TestNullAmountAlert:
    def test_wording_carries_operator_context(self):
        subject, body = null_amount_alert("job-1", "pierce/WA", 1, 6, ["nid-1"])
        assert subject == "Auction leads without Default Owed: 1/6 (pierce/WA)"
        assert "job-1" in body and "nid-1" in body and "parser" in body
        assert "repair_trustee_sale_from_notices.py" in body

    def test_no_ids(self):
        _, body = null_amount_alert("job-1", "pierce/WA", 2, 2, [])
        assert "n/a" in body


class TestRepairScope:
    def test_amount_sql_is_type_parameterized(self):
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "repair_trustee_sale_from_notices.py"
        spec = importlib.util.spec_from_file_location("repair", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "sc.record_type = ANY(:types)" in str(mod._AMOUNT_CANDIDATES)
        assert "sc.record_type = ANY(:types)" in str(mod._AMOUNT_REPAIR)
        assert mod._AMOUNT_TYPES_DEFAULT == ["trustee_sale"]
        assert mod._AMOUNT_TYPES_WIDE == ["trustee_sale", "pre_foreclosure"]
        rows = [
            SimpleNamespace(record_type="trustee_sale", nts_match_confidence=Decimal("1.00")),
            SimpleNamespace(record_type="pre_foreclosure", nts_match_confidence=Decimal("0.90")),
            SimpleNamespace(record_type="pre_foreclosure", nts_match_confidence=None),
        ]
        assert mod.amount_breakdown(rows) == {"trustee_sale/exact": 1, "pre_foreclosure/fuzzy": 1,
                                              "pre_foreclosure/exact": 1}


def _seed(db, ts: str, *, amount, source_url: str, active: bool = True, fetched_days_ago: int = 5):
    """Insert one real-shaped notice row via the crawler's own upsert."""
    parsed = nts.parse_nts_notice(_load("nts_tacoma_matured_obligation.txt"))
    parsed["ts_number"] = ts
    row = nts.notice_to_row(parsed, source_url=source_url, today=date.today())
    row["principal_owing"] = amount
    row["auction_date"] = date.today() + timedelta(days=30)
    row["is_active"] = active
    row["fetched_at"] = datetime.now(UTC) - timedelta(days=fetched_days_ago)
    nts_crawler._upsert_notice(db, NtsNotice, row)
    db.commit()


def _cleanup(db):
    db.execute(text("DELETE FROM nts_notices WHERE ts_number LIKE :p"), {"p": _TS_PREFIX + "%"})
    db.commit()


class TestResweep:
    """Real Postgres + real parser; only the HTTP fetch is an injected function."""

    def _run(self, db, fetch):
        return nts_crawler._resweep_null_amount_notices(
            db, NtsNotice, source=nts.SOURCE, county=nts.COUNTY, today=date.today(),
            fetch=fetch, parse=nts.parse_nts_notice, notice_to_row=nts.notice_to_row,
        )

    def test_reparse_fills_amount(self):
        ts = _TS_PREFIX + "FILL"
        url = "https://www.tacomadailyindex.com/test/" + ts.lower()
        with system_sync_session() as db:
            _cleanup(db)
            _seed(db, ts, amount=None, source_url=url)
            text_body = _load("nts_tacoma_matured_obligation.txt").replace("WA-26-1050840-BB", ts)
            counts = self._run(db, lambda u: (200, text_body))
            db.commit()
            assert counts["attempted"] == 1 and counts["updated"] == 1
            amt = db.execute(text("SELECT principal_owing FROM nts_notices WHERE ts_number = :t"),
                             {"t": ts}).scalar()
            assert amt == Decimal("575150.38")
            # retried again only after the min-age window: nothing to do now
            assert self._run(db, lambda u: (200, text_body))["attempted"] == 0
            _cleanup(db)

    def test_404_touches_fetched_at_but_keeps_the_notice_active(self):
        ts = _TS_PREFIX + "GONE"
        with system_sync_session() as db:
            _cleanup(db)
            _seed(db, ts, amount=None, source_url="https://www.tacomadailyindex.com/test/gone")
            counts = self._run(db, lambda u: (404, ""))
            db.commit()
            assert counts == {"attempted": 1, "updated": 0, "unchanged_null": 0, "not_found": 1, "errors": 0}
            active, fetched = db.execute(
                text("SELECT is_active, fetched_at FROM nts_notices WHERE ts_number = :t"), {"t": ts}
            ).one()
            assert active is True
            assert (datetime.now(UTC) - fetched).total_seconds() < 120
            _cleanup(db)

    def test_still_no_amount_is_counted_not_updated(self):
        ts = _TS_PREFIX + "NULL"
        with system_sync_session() as db:
            _cleanup(db)
            _seed(db, ts, amount=None, source_url="https://www.tacomadailyindex.com/test/null")
            no_amount = _load("nts_tacoma_matured_obligation.txt").replace(
                "WA-26-1050840-BB", ts).replace("$575,150.38", "as stated in the note")
            counts = self._run(db, lambda u: (200, no_amount))
            db.commit()
            assert counts["unchanged_null"] == 1 and counts["updated"] == 0
            _cleanup(db)

    def test_rows_with_an_amount_are_not_swept(self):
        ts = _TS_PREFIX + "HASAMT"
        with system_sync_session() as db:
            _cleanup(db)
            _seed(db, ts, amount=Decimal("1.00"), source_url="https://www.tacomadailyindex.com/test/x")
            assert self._run(db, lambda u: (200, ""))["attempted"] == 0
            _cleanup(db)


class TestDashedTwinRetired:
    def test_upsert_of_clean_key_deactivates_dashed_twin(self):
        clean = _TS_PREFIX + "TWIN"
        with system_sync_session() as db:
            _cleanup(db)
            _seed(db, clean + "-", amount=Decimal("1.00"), source_url="https://www.tacomadailyindex.com/t/a")
            _seed(db, clean, amount=Decimal("1.00"), source_url="https://www.tacomadailyindex.com/t/a")
            rows = dict(db.execute(
                text("SELECT ts_number, is_active FROM nts_notices WHERE ts_number LIKE :p"),
                {"p": clean + "%"}).fetchall())
            assert rows == {clean + "-": False, clean: True}
            _cleanup(db)
