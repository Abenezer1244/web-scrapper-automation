"""OCR of recorded Notice-of-Trustee-Sale scans: isolation contract and review evidence.

Everything runs for real: the child process (python -I nts_ocr_child.py), and in place
of tesseract a small executable written per test that behaves like one (writes TSV,
floods, hangs with a descendant, or fails), so the parent's limits are exercised
against actual processes. PDFs are built in Chelan's recorder shape (one 1-bit Flate
image per page, as read live 2026-11-12) or deliberately outside it.
"""
import io
import os
import stat
import sys
import time
import zlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ByteStringObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from src.scrapers.sources.nts_recorder_ocr import (
    MIN_FIELD_CONF,
    field_evidence,
    ocr_pdf,
    ocr_text,
    read_notice,
    review_flags,
)

_TSV_HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


CHELAN_DRAW = b"q 1 0 0 1 0 0 cm 612 0 0 835 0 0 cm /Im0 Do Q \r\n"
WHITE_FIRST = ArrayObject([NameObject("/Indexed"), NameObject("/DeviceRGB"), NumberObject(1),
                           ByteStringObject(b"\xff\xff\xff\x00\x00\x00")])


def recorder_pdf(pages: int = 2, width: int = 850, height: int = 1100, *, draw: bytes | None = CHELAN_DRAW,
                 colorspace=None, bits: bytes = b"\x00") -> bytes:
    """A PDF in the recorder's shape (Chelan, live 2026-11-12): per page, /ProcSet + one
    1-bit Flate image drawn full-page by the content stream, palette index 0 = WHITE.
    ``bits`` fills the image (0x00 = all index 0 = a white page)."""
    w = PdfWriter()
    raw = zlib.compress(bits * (((width + 7) // 8) * height))
    for _ in range(pages):
        page = w.add_blank_page(612, 835)  # Chelan's media box: the image fills it
        img = StreamObject()
        img._data = raw
        img.update({
            NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(width), NameObject("/Height"): NumberObject(height),
            NameObject("/BitsPerComponent"): NumberObject(1),
            NameObject("/ColorSpace"): (colorspace(w) if callable(colorspace) else colorspace)
            if colorspace is not None else WHITE_FIRST,
            NameObject("/Filter"): NameObject("/FlateDecode"),
        })
        ref = w._add_object(img)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/ProcSet"): ArrayObject([NameObject("/PDF"), NameObject("/ImageB")]),
            NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): ref}),
        })
        if draw is not None:
            content = StreamObject()
            content._data = draw
            page[NameObject("/Contents")] = w._add_object(content)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _tool(tmp_path: Path, name: str, body: str) -> str:
    """An executable standing in for tesseract: argv = [png, outbase, -l, eng, tsv]."""
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        cmd = tmp_path / f"{name}.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(cmd)
    exe = tmp_path / name
    exe.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


_OK = f'''
import sys
from PIL import Image
png, out = sys.argv[1], sys.argv[2]
im = Image.open(png).convert("L")
w, h = im.size
black = sum(im.histogram()[:128]) / (w * h)
open(out + ".tsv", "w").write({_TSV_HEAD!r} + "5\\t1\\t1\\t1\\t1\\t1\\t0\\t0\\t1\\t1\\t91.5\\tTrustee\\n"
    + "5\\t1\\t1\\t1\\t1\\t2\\t0\\t0\\t1\\t1\\t88.0\\t" + f"{{w}}x{{h}}:{{black:.2f}}" + "\\n")
'''


def test_a_recorder_shaped_pdf_is_ocrd_page_by_page(tmp_path):
    res = ocr_pdf(recorder_pdf(pages=2), tesseract=_tool(tmp_path, "tess_ok", _OK))
    assert res.ok and res.pages == 2 and len(res.pdf_sha256) == 64
    # all-zero bits under Chelan's white-first palette arrive as a WHITE page (inverted)
    assert [w[2] for w in res.words] == ["Trustee", "850x1100:0.00", "Trustee", "850x1100:0.00"]
    assert ocr_text(res.words) == "Trustee 850x1100:0.00\nTrustee 850x1100:0.00"


def test_a_gray_image_is_not_inverted(tmp_path):
    res = ocr_pdf(recorder_pdf(1, colorspace=NameObject("/DeviceGray")),
                  tesseract=_tool(tmp_path, "tess_ok", _OK))
    assert res.words[1][2] == "850x1100:1.00"  # 0 bits are black in DeviceGray


def _with_annotation(pdf: bytes) -> bytes:
    """The same document with a full-page annotation drawn over the image."""
    from pypdf import PdfReader
    from pypdf.annotations import FreeText

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    w.add_annotation(page_number=0, annotation=FreeText(text="SALE 1/1/2030", rect=(0, 0, 612, 835)))
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def _pil_pdf(width: int, height: int) -> bytes:
    b = io.BytesIO()
    Image.new("1", (width, height), 1).save(b, "PDF")
    return b.getvalue()


def _blank(pages: int, encrypt: bool = False) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    if encrypt:
        w.encrypt("secret")
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


@pytest.mark.parametrize("pdf,reason", [
    (b"<html>not a pdf</html>", "not a pdf"),
    (_blank(21), "more than 20 pages"),
    (_blank(1, encrypt=True), "pdf encrypted"),
    (_blank(1), "unsupported pdf: page"),            # no image at all
    (_pil_pdf(850, 1100), "unsupported pdf"),         # an image, but not the recorder's shape
    (recorder_pdf(1, 13000, 13000), "unsupported pdf: image size out of bounds"),  # 169 M px
    (b"%PDF-1.7 truncated garbage", "unreadable pdf"),
    (recorder_pdf(1, draw=None), "unsupported pdf: page contents"),  # image never drawn
    (recorder_pdf(1, draw=b"q 612 0 0 835 0 0 cm /Im0 Do Q 0 0 m 612 792 l S"),
     "unsupported pdf: page does not draw exactly its one image"),     # an overlay
    (recorder_pdf(1, draw=b"BI /W 1 /H 1 /BPC 1 ID x EI q 612 0 0 835 0 0 cm /Im0 Do Q"),
     "unsupported pdf: page does not draw exactly its one image"),     # an inline image
    (recorder_pdf(1, colorspace=NameObject("/DeviceRGB")), "unsupported pdf: image colour space"),
    (recorder_pdf(1, colorspace=ArrayObject([NameObject("/Indexed"), NameObject("/DeviceRGB"),
                                             NumberObject(1), ByteStringObject(b"\xff\x00\x00\x00\x00\x00")])),
     "unsupported pdf: image colour space"),                            # red, not white
    (recorder_pdf(1, draw=b"q 0 0 0 0 0 0 cm /Im0 Do Q"),
     "unsupported pdf: image does not cover exactly the page"),         # zero scale: invisible
    (recorder_pdf(1, draw=b"q 612 0 0 835 700 0 cm /Im0 Do Q"),
     "unsupported pdf: image does not cover exactly the page"),         # moved off the page
    (recorder_pdf(1, draw=b"q 306 0 0 417 0 0 cm /Im0 Do Q"),
     "unsupported pdf: image does not cover exactly the page"),         # a quarter of the page
    (recorder_pdf(1, draw=b"q 0 835 -612 0 612 0 cm /Im0 Do Q"),
     "unsupported pdf: image does not cover exactly the page"),         # rotated
    (recorder_pdf(1, draw=b"q 0 0 0 0 0 0 cm 1" + b"0" * 400 + b" 0 0 1 0 0 cm /Im0 Do Q"),
     "unsupported pdf: drawing numbers are not finite"),               # overflow x 0 = NaN
    (_with_annotation(recorder_pdf(1)), "unsupported pdf: page has annotations"),
], ids=["html", "21-pages", "encrypted", "no-image", "pil-image", "bomb-169M-px", "truncated",
        "undrawn-image", "overlay", "inline-image", "rgb", "red-palette",
        "zero-scale", "off-page", "quarter-page", "rotated-matrix", "nan-matrix", "annotation"])
def test_documents_outside_the_accepted_shape_are_rejected_before_ocr(tmp_path, pdf, reason):
    ran = tmp_path / "ran"
    tool = _tool(tmp_path, "tess_mark", f"open({str(ran)!r}, 'w').write('x')\n")
    res = ocr_pdf(pdf, tesseract=tool)
    assert res.ok is False and res.reason.startswith(reason) and res.words == []
    assert not ran.exists()  # tesseract never ran


def test_chelans_two_matrix_drawing_is_accepted(tmp_path):
    # Chelan writes "q 1 0 0 1 0 0 cm 612 0 0 835 0 0 cm /Im0 Do Q": identity, then scale.
    res = ocr_pdf(recorder_pdf(1, draw=b"q 1 0 0 1 0 0 cm 612 0 0 835 0 0 cm /Im0 Do Q"),
                  tesseract=_tool(tmp_path, "tess_ok", _OK))
    assert res.ok, res.reason


def test_a_palette_stream_is_never_decoded():
    def stream_palette(writer):
        palette = StreamObject()
        palette._data = zlib.compress(b"\x00" * 10_000_000)  # 10 MB when inflated
        palette[NameObject("/Filter")] = NameObject("/FlateDecode")
        return ArrayObject([NameObject("/Indexed"), NameObject("/DeviceRGB"), NumberObject(1),
                            writer._add_object(palette)])

    res = ocr_pdf(recorder_pdf(1, colorspace=stream_palette), tesseract="tesseract-that-does-not-exist")
    assert (res.ok, res.reason) == (False, "unsupported pdf: image colour space is not plain black and white")


def test_image_data_longer_than_its_declared_size_is_rejected(tmp_path):
    pdf = recorder_pdf(1, 850, 1100).replace(b"/Height 1100", b"/Height 1000")
    res = ocr_pdf(pdf, tesseract=_tool(tmp_path, "tess_ok", _OK))
    assert (res.ok, res.reason) == (False, "unsupported pdf: stream data does not match its declared size")


def test_an_oversized_pdf_is_rejected():
    res = ocr_pdf(b"%PDF-" + b"0" * (20 * 1024 * 1024), tesseract="tesseract-that-does-not-exist")
    assert (res.ok, res.reason) == (False, "pdf too large")


def test_a_missing_ocr_binary_is_a_reason_not_a_crash():
    res = ocr_pdf(recorder_pdf(1), tesseract=str(Path("no-such-dir") / "tesseract"))
    assert (res.ok, res.reason) == (False, "tesseract not found")


def test_a_failing_ocr_binary_is_a_reason(tmp_path):
    res = ocr_pdf(recorder_pdf(1), tesseract=_tool(tmp_path, "tess_fail", "import sys; sys.exit(1)\n"))
    assert (res.ok, res.reason) == (False, "page 1 ocr failed (1)")


def test_flooding_ocr_output_is_refused_unread(tmp_path):
    body = "import sys\nopen(sys.argv[2] + '.tsv', 'w').write('x' * (3 * 1024 * 1024))\n"
    res = ocr_pdf(recorder_pdf(1), tesseract=_tool(tmp_path, "tess_flood", body))
    assert (res.ok, res.reason) == (False, "page 1 ocr output too large")


def test_an_overrun_kills_the_whole_process_tree_and_removes_the_scans(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    body = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(300)\n"
    )
    before = set(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp").glob("nts_ocr_*"))
    started = time.monotonic()
    res = ocr_pdf(recorder_pdf(1), tesseract=_tool(tmp_path, "tess_hang", body), wall_s=8, page_s=120)
    assert (res.ok, res.reason) == (False, "ocr timed out")
    assert time.monotonic() - started < 60
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 20
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not _alive(pid), "tesseract's descendant outlived the timeout"
    after = set(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp").glob("nts_ocr_*"))
    assert after <= before, "a work directory with page scans was left behind"


def test_a_page_timeout_also_kills_the_tree_and_removes_the_scans(tmp_path):
    pidfile = tmp_path / "descendant.pid"
    body = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(300)\n"
    )
    tmproot = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    before = set(tmproot.glob("nts_ocr_*"))
    res = ocr_pdf(recorder_pdf(1), tesseract=_tool(tmp_path, "tess_hang", body), wall_s=120, page_s=6)
    assert (res.ok, res.reason) == (False, "page 1 ocr timed out")
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 20
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not _alive(pid), "tesseract's descendant outlived the page timeout"
    assert set(tmproot.glob("nts_ocr_*")) <= before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_FSIZE is Linux-only")
def test_a_runaway_writer_is_stopped_at_the_file_size_limit(tmp_path):
    body = ("import sys\nf = open(sys.argv[2] + '.tsv', 'w')\n"
            "while True:\n    f.write('x' * 1048576)\n")
    res = ocr_pdf(recorder_pdf(1), tesseract=_tool(tmp_path, "tess_disk", body), wall_s=120, page_s=60)
    assert res.ok is False  # the write fails at 16 MiB instead of filling the disk


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess

        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True, check=False).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead for our purposes
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split()[2]
    except OSError:
        return True


def test_read_notice_passes_a_rejection_through_without_fields():
    out = read_notice(b"nope", date(2026, 10, 2))
    assert out == {"ok": False, "reason": "not a pdf", "pdf_sha256": out["pdf_sha256"]}


# ── evidence and review flags (words as tesseract TSV gives them) ───────────────

_WORDS = [
    [1, "1.1.1.1", "TS", 96.0], [1, "1.1.1.1", "No.:", 95.0], [1, "1.1.1.1", "WA-26-1035144-SW", 93.0],
    [1, "1.2.1.1", "Parcel", 95.0], [1, "1.2.1.1", "222014846080", 91.0],
    [2, "2.4.1.2", "Trustee", 96.0], [2, "2.4.1.2", "will", 96.0], [2, "2.4.1.2", "on", 96.0],
    [2, "2.4.1.2", "2/19/2027", 88.0],
    [2, "2.6.1.1", "$123,456.78", 42.0],
]
_PARSED = {
    "ts_number": "WA-26-1035144-SW", "parcel": "222014846080",
    "auction_date": "2/19/2027", "principal_owing": Decimal("123456.78"),
}


def test_field_evidence_locates_each_value_as_printed_on_one_line():
    ev = field_evidence(_PARSED, _WORDS)
    assert ev["ts_number"] == {"value": "WA-26-1035144-SW", "min_conf": 93.0, "pages": [1],
                               "lines": ["1.1.1.1"], "occurrences": 1}
    assert (ev["auction_date"]["min_conf"], ev["auction_date"]["pages"]) == (88.0, [2])
    assert ev["principal_owing"]["min_conf"] == 42.0  # "$123,456.78" is 123,456.78 as printed


def test_an_amount_printed_with_other_punctuation_is_not_evidence():
    words = [[2, "2.6.1.1", "$1234,56", 99.0]]
    ev = field_evidence({"principal_owing": Decimal("1234.56")}, words)
    assert ev["principal_owing"]["min_conf"] is None


def test_a_spelled_out_date_must_appear_on_one_line():
    words = [[2, "2.4.1.1", "February", 95.0], [2, "2.4.1.1", "19,", 95.0], [2, "2.9.1.1", "2027", 95.0]]
    ev = field_evidence({"auction_date": "February 19, 2027"}, words)
    assert ev["auction_date"]["min_conf"] is None  # its words are split across lines


def test_a_value_printed_twice_reports_its_weakest_reading_and_is_flagged():
    words = [[1, "1.3.1.1", "$123,456.78", 99.0], [2, "2.6.1.1", "$123,456.78", 41.0]]
    parsed = {"principal_owing": Decimal("123456.78")}
    ev = field_evidence(parsed, words)
    assert (ev["principal_owing"]["min_conf"], ev["principal_owing"]["occurrences"]) == (41.0, 2)
    flags = review_flags(parsed, ev, date(2026, 10, 2))
    assert any("appears 2 times" in f for f in flags)
    assert any(f.startswith("principal_owing: low OCR confidence") for f in flags)


def test_low_confidence_unfound_and_missing_values_are_flagged_never_corrected():
    parsed = {**_PARSED, "parcel": "222014846088"}  # not in the words as read
    del parsed["ts_number"]
    flags = review_flags(parsed, field_evidence(parsed, _WORDS), date(2026, 10, 2))
    assert "ts_number: not read from the notice" in flags
    assert any(f.startswith("principal_owing: low OCR confidence") for f in flags)
    assert any(f.startswith("parcel: not found as printed") for f in flags)
    assert 42.0 < MIN_FIELD_CONF


@pytest.mark.parametrize("sale,expect", [
    ("2/19/2027", None),                      # 140 days after a 10/02/2026 recording
    ("February 19, 2027", None),              # the spelled-out form reads the same
    ("12/19/2026", "statutory compliance not determined"),  # 78 days: below the ordinary 90
    ("2/19/2028", "not a statutory limit"),   # a likely misread year: heuristic only
    ("2/31/2027", "is not a calendar date"),  # a high-confidence impossible date
])
def test_sale_dates_are_checked_against_recording(sale, expect):
    flags = review_flags({**_PARSED, "auction_date": sale}, {}, date(2026, 10, 2))
    date_flags = [f for f in flags if f.startswith("auction_date")]
    if expect is None:
        assert date_flags == []
    else:
        assert any(expect in f for f in date_flags)


def test_stored_fields_never_carry_the_borrower(tmp_path):
    out = read_notice(recorder_pdf(1), date(2026, 10, 2), tesseract=_tool(tmp_path, "tess_ok", _OK))
    assert out["ok"] and "grantor" not in out["fields"]
