"""King LandmarkWeb probate row parsing — semantic field extraction.

Every ``row`` below is a VERBATIM DataTables row captured from King County's live
recorder JSON (``/LandmarkWeb/Search/GetSearchResults``) for 06/04/2026-09/02/2026,
the window behind the "Test 7" data-quality audit. Column 5 = grantor,
6 = grantee, 7 = record date, 8 = doc type, 12 = recording number, 14 = legal.

These assert the SEMANTIC outcome — which real value lands in which field — rather
than that some string is forbidden.
"""
from src.scrapers.king_wa_probate import KingCountyLandmarkWebScraper


def _row(**over):
    """A real King row skeleton; override the columns a case cares about."""
    base = {
        "0": "nobreak_  1",
        "3": "V",
        "4": "hidden_",
        "5": "",
        "6": "",
        "7": "nobreak_06/08/2026",
        "8": "nobreak_DEATH CERTIFICATE",
        "9": "NONE",
        "10": "",
        "11": "0000",
        "12": "nobreak_20260608000417",
        "13": "nobreak_",
        "14": "",
        "15": "hidden_legalfield_",
        "21": "hidden_legalfield_",
        "22": "hidden_29288732",
    }
    base.update(over)
    return base


def _parse(rows):
    return KingCountyLandmarkWebScraper(record_type="probate")._parse_json_results(rows)


def test_ordinary_row_maps_grantor_to_party_and_grantee_to_heirs():
    (rec,) = _parse([_row(
        **{"5": "BANEZ MATILDE UMIPIG", "6": "BANEZ JOSELITO U",
           "14": "PID: 3424049092 QTR: NW SEC: 34 TWP: 24 RGE: 4"}
    )])
    assert rec.party_name == "BANEZ MATILDE UMIPIG"
    assert rec.heirs == "BANEZ JOSELITO U"
    assert rec.parcel_id == "3424049092"
    assert rec.date_recorded == "06/08/2026"
    assert rec.doc_type == "DEATH CERTIFICATE"
    assert rec.enrichment_data["instrument_number"] == "20260608000417"


def test_placeholder_grantee_does_not_become_an_heir():
    # 101 of 204 live rows in this window. The decedent is unaffected; the
    # recorder's "recorded to the public" placeholder must not surface as a person.
    (rec,) = _parse([_row(
        **{"5": "ELTING EMILY WILLIAMS", "6": "PUBLIC",
           "14": "PID: 3424049092 QTR: NW SEC: 34 TWP: 24 RGE: 4"}
    )])
    assert rec.party_name == "ELTING EMILY WILLIAMS"
    assert rec.heirs is None


def test_reversed_row_puts_the_decedent_in_party_name():
    # Instrument 20260828001142, verbatim: King indexed the placeholder as grantor
    # and the DECEDENT as grantee. Corroborated at the assessor — parcel
    # 3276080220's owner is "TRUJILLO CHUCK+PATSY".
    (rec,) = _parse([_row(
        **{"5": "PUBLIC", "6": "TRUJILLO CHARLES JAMES",
           "7": "nobreak_08/28/2026", "12": "nobreak_20260828001142",
           "14": "PID: 3276080220 QTR: NE SEC: 17 TWP: 21 RGE: 5 SUB: HIDDEN VALLEY VISTA"}
    )])
    assert rec.party_name == "TRUJILLO CHARLES JAMES"
    assert rec.heirs is None
    assert rec.parcel_id == "3276080220"


def test_reversed_agency_row_puts_the_decedent_in_party_name():
    # Instrument 20260715000926, verbatim — the trailing "<STATE> HEALTH DEPARTMENT"
    # word order. Note the county's own legal carries an ELEVEN-digit PID; the
    # scraper preserves it verbatim (never fabricates a 10-digit guess) and the
    # enrichment's parcel-echo check is what stops it attaching a wrong address.
    (rec,) = _parse([_row(
        **{"5": "WASHINGTON STATE HEALTH DEPARTMENT", "6": "REINKE NORMAN LEONARD",
           "7": "nobreak_07/15/2026", "12": "nobreak_20260715000926",
           "14": "PID: 64116000027 QTR: NW SEC: 29 TWP: 26 RGE: 4 SUB: ORR H E PARK"}
    )])
    assert rec.party_name == "REINKE NORMAN LEONARD"
    assert rec.heirs is None
    assert rec.parcel_id == "64116000027"
    assert rec.legal_description == (
        "PID: 64116000027 QTR: NW SEC: 29 TWP: 26 RGE: 4 SUB: ORR H E PARK"
    )


def test_row_with_no_party_on_either_side_is_dropped():
    # Guard #2: a probate lead with no decedent is unusable and would still be
    # billed, so it must not ship.
    assert _parse([_row(
        **{"5": "WASHINGTON STATE DEPT OF HEALTH", "6": "PUBLIC",
           "14": "PID: 3424049092 QTR: NW SEC: 34 TWP: 24 RGE: 4"}
    )]) == []


def test_row_without_a_pid_is_dropped_and_does_not_shift_its_neighbours():
    # 83 of the 204 live rows have an empty legal cell. Each row is parsed from its
    # OWN dict keys, so a dropped row can never shift another row's columns.
    recs = _parse([
        _row(**{"5": "HUNT DANIEL LEWIS", "6": "HUNT KERRI", "14": ""}),
        _row(**{"5": "SERONKO ROBERT LEE", "6": "BOEDE JACK HENRY JR",
                "7": "nobreak_07/09/2026", "12": "nobreak_20260709000123",
                "14": "PID: 1234567890 QTR: SW SEC: 26 TWP: 21 RGE: 4"}),
    ])
    assert len(recs) == 1
    assert recs[0].party_name == "SERONKO ROBERT LEE"
    assert recs[0].heirs == "BOEDE JACK HENRY JR"
    assert recs[0].parcel_id == "1234567890"
    assert recs[0].date_recorded == "07/09/2026"


def test_stacked_grantee_keeps_the_real_heir_and_drops_the_placeholder():
    (rec,) = _parse([_row(
        **{"5": "SINGH GURDEV", "6": "KAUR RAJWANT<div class='nameSeperator'></div>PUBLIC",
           "14": "PID: 3424049092 QTR: NW SEC: 34 TWP: 24 RGE: 4"}
    )])
    assert rec.party_name == "SINGH GURDEV"
    assert rec.heirs == "KAUR RAJWANT"
