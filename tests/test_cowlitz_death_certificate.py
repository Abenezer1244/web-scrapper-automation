"""Cowlitz death_certificate: recorded death certificates leave probate for their own type (164).

Live labels (Cowlitz recorder, 10/15-11/14/2026): "Death Certificate" (agency grantor,
decedent grantee; or decedent grantor, "PUBLIC THE" grantee) and "Death Certificate to
Perfect Death Deed". Names below are redacted.
"""
import importlib.util
from pathlib import Path

import pytest

from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.templates.laserfiche_weblink import _DOC_TYPE_MAP, LaserficheWebLinkScraper

_BASE = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"
_MIG = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "164_cowlitz_death_certificate.py"


def _scraper(record_type):
    return LaserficheWebLinkScraper(_BASE, "cowlitz", "WA", record_type=record_type)


def _row(doc_type):
    return {"afn": "3799666-0", "date": "11/10/2026", "doc_type": doc_type, "volume": "",
            "page_num": "", "href": "/WLAUDPublic/DocView.aspx?id=1"}


def test_death_certificates_are_their_own_type_not_probate():
    dc, probate = _scraper("death_certificate"), _scraper("probate")
    for label in ("Death Certificate", "Death Certificate to Perfect Death Deed"):
        assert dc._type_matches(label), label
        assert not probate._type_matches(label), label
    for label in ("Affidavit Lack of Probate", "Transfer on Death Deed"):
        assert probate._type_matches(label) and not dc._type_matches(label), label
    assert "DEATH CERTIFICATE" not in _DOC_TYPE_MAP["probate"]


def test_agency_grantor_promotes_the_decedent_grantee():
    fields = {"Rec Doc AFN": ["3799666"], "Rec Doc Grantor": ["STATE OF WASHINGTON DEPARMENT OF HEALTH"],
              "Rec Doc Grantee": ["SAMPLE DECEDENT W"], "Rec Doc Parcel": ["63174069"]}
    rec = _scraper("death_certificate")._build_record(_row("Death Certificate"), fields)
    assert rec.party_name == "SAMPLE DECEDENT W"
    assert rec.parcel_id == "63174069"


def test_decedent_grantor_keeps_the_decedent_and_drops_public_the():
    fields = {"Rec Doc AFN": ["3798592"], "Rec Doc Grantor": ["SAMPLE DECEDENT L"],
              "Rec Doc Grantee": ["PUBLIC THE"], "Rec Doc Parcel": ["63174069"]}
    rec = _scraper("death_certificate")._build_record(_row("Death Certificate"), fields)
    assert rec.party_name == "SAMPLE DECEDENT L"
    assert "PUBLIC" not in (rec.heirs or "")


def test_a_certificate_naming_no_person_is_dropped():
    fields = {"Rec Doc AFN": ["1"], "Rec Doc Grantor": ["STATE OF WASHINGTON"],
              "Rec Doc Grantee": ["PUBLIC THE"], "Rec Doc Parcel": ["63174069"]}
    assert _scraper("death_certificate")._build_record(_row("Death Certificate"), fields) is None


def test_migration_chains_onto_163_and_only_touches_cowlitz():
    spec = importlib.util.spec_from_file_location("_mig164", _MIG)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert (mig.revision, mig.down_revision) == ("164", "163")
    src = _MIG.read_text(encoding="utf-8")
    assert src.count("lower(county) = 'cowlitz'") == 2
    assert "death_certificate" in _DOC_TYPE_MAP


class _OneDayPortal(LaserficheWebLinkScraper):
    """scrape() end to end on fixed rows and their entry properties (as read live)."""

    def __init__(self, entries):
        super().__init__(_BASE, "cowlitz", "WA", record_type="death_certificate")
        self.entries = entries

    async def _search(self, date_from, date_to):
        return len(self.entries), [dict(e["row"]) for e in self.entries]

    async def _read_entry(self, row):
        for e in self.entries:
            if e["row"]["afn"] == row["afn"]:
                if isinstance(e["fields"], Exception):
                    raise e["fields"]
                return e["fields"]
        raise AssertionError(row["afn"])


def _entry(afn, doc_type, parcels, decedent="SAMPLE DECEDENT W"):
    fields = {"Rec Doc AFN": [afn.split("-")[0]], "Rec Doc Type Desc": [doc_type],
              "Rec Doc Grantor": ["STATE OF WASHINGTON DEPARMENT OF HEALTH"], "Rec Doc Grantee": [decedent]}
    if parcels:
        fields["Rec Doc Parcel"] = parcels
    return {"row": {**_row(doc_type), "afn": afn}, "fields": fields}



@pytest.mark.asyncio
async def test_scrape_keeps_one_parcel_holds_several_and_drops_none():
    portal = _OneDayPortal([
        _entry("1-0", "Death Certificate", ["63174069"]),
        _entry("2-0", "Death Certificate", ["02363", "21550"], "SAMPLE DECEDENT B"),
        _entry("3-0", "Death Certificate", [], "SAMPLE DECEDENT C"),
        _entry("4-0", "Transfer on Death Deed", ["05635"]),  # probate, not this type
    ])
    recs = await portal.scrape("11/10/2026", "11/10/2026")
    by_afn = {r.enrichment_data["instrument_number"]: r for r in recs}
    assert set(by_afn) == {"1-0", "2-0"}
    assert by_afn["1-0"].parcel_id == "63174069" and by_afn["1-0"].party_name == "SAMPLE DECEDENT W"
    held = by_afn["2-0"]
    assert held.parcel_id is None and held.enrichment_data["recording_apn_lookup"] == "ambiguous"
    assert held.enrichment_data["apn_candidates"] == ["02363", "21550"]


@pytest.mark.asyncio
async def test_scrape_fails_when_a_certificate_cannot_be_read():
    portal = _OneDayPortal([
        _entry("1-0", "Death Certificate", ["63174069"]),
        {"row": {**_row("Death Certificate"), "afn": "2-0"},
         "fields": ScraperExecutionError("cowlitz", "Laserfiche WebLink", "never settled")},
    ])
    with pytest.raises(ScraperExecutionError, match="never settled"):
        await portal.scrape("11/10/2026", "11/10/2026")
