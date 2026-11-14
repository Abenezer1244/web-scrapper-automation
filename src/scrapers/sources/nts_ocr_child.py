"""Child process for nts_recorder_ocr: OCR one untrusted recorder PDF.

Run as ``python -I <this file> <workdir> <tesseract> <per-page seconds>`` with the PDF
at ``<workdir>/in.pdf``. Isolated (no repo imports), so a hostile document is decoded
in a process the parent can kill; the parent owns <workdir> and removes it.

Only one document shape is accepted, the one Chelan's recorder produces (both live NTS
documents read 2026-11-12):
- page resources: /ProcSet and /XObject only, the XObject exactly one image;
- page contents: exactly ``q <matrix> cm [<matrix> cm] /<that image> Do Q``, so the
  bitmap OCR'd is the one page shown, alone (no overlay, clip or inline image);
- the image: 1-bit, /FlateDecode only, no masks or decode arrays, colour space
  /DeviceGray or a two-entry pure black/white /Indexed palette (Chelan's is
  [/Indexed /DeviceRGB 1 <FFFFFF000000>]: index 0 is WHITE, so it is inverted).
Image and content streams are inflated here with an output ceiling, so nothing else is
ever decoded. Anything else is "unsupported pdf".

Tesseract runs with a fixed argument list, no shell, one thread, a timeout (its whole
process tree is killed on overrun), output to a file size-checked before it is read,
and stdout/stderr discarded. On Linux the child also caps its address space and the
size of any file it or tesseract writes. The result goes to ``<workdir>/result.json``:
{"ok": true, "pages": n, "words": [[page, line_key, text, conf], ...]} or
{"ok": false, "reason": "..."}.
"""
import io
import json
import math
import os
import re
import signal
import subprocess
import sys
import zlib

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_IMAGE_PIXELS = 12_000_000       # a 300 dpi letter page is ~8.9 M
MAX_STREAM_BYTES = 5 * 1024 * 1024  # one compressed page image
MAX_CONTENT_BYTES = 4096            # a page's drawing commands (Chelan's: ~50 bytes)
MAX_TSV_BYTES = 2 * 1024 * 1024     # tesseract's output for one page
MAX_FILE_BYTES = 16 * 1024 * 1024   # any file written while OCR runs (Linux)
MAX_WORDS = 50_000
_ADDRESS_SPACE = 1536 * 1024 * 1024
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)"
_MATRIX = rf"((?:{_NUM}\s+){{6}})cm\s+"
_DRAW_ONE_IMAGE = re.compile(rf"^\s*q\s+{_MATRIX}(?:{_MATRIX})?/(\S+)\s+Do\s+Q\s*$")
_PAGE_TOLERANCE = 2.0  # points: how closely the drawn image must cover the page
_WHITE, _BLACK = b"\xff\xff\xff", b"\x00\x00\x00"


class UnsupportedPdfError(Exception):
    """The document is not the one shape this reader accepts."""


def _result(workdir: str, obj) -> None:
    with open(os.path.join(workdir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _bounded_inflate(raw: bytes, limit: int, exact: int | None = None) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(raw, limit + 1)
    if len(out) > limit or d.unconsumed_tail or (exact is not None and len(out) != exact):
        raise UnsupportedPdfError("stream data does not match its declared size")
    return out


def _page_content(page) -> bytes:
    contents = page.get("/Contents")
    obj = contents.get_object() if contents is not None else None
    if obj is None or isinstance(obj, list) or not hasattr(obj, "get"):
        raise UnsupportedPdfError("page contents not a single stream")
    raw = obj._data
    if len(raw) > MAX_CONTENT_BYTES:
        raise UnsupportedPdfError("page contents too large")
    filt = obj.get("/Filter")
    if filt is None:
        return raw
    if filt in ("/FlateDecode", ["/FlateDecode"]):
        return _bounded_inflate(raw, MAX_CONTENT_BYTES)
    raise UnsupportedPdfError("page contents use an unsupported filter")


def _finite(values) -> list[float]:
    """Every number must be finite: NaN compares false with everything, so an overflow
    (1e400 x 0) would otherwise slip through each bound below."""
    out = list(values)
    if not all(math.isfinite(v) for v in out):
        raise UnsupportedPdfError("drawing numbers are not finite")
    return out


def _check_covers_page(page, matrices: list[str]) -> None:
    """The drawn image must fill the page exactly (no crop, no rotation): its composed
    matrix is axis-aligned and positive and maps the unit square onto the MediaBox, so
    the bitmap OCR'd is the page a reader sees, all of it and nothing more."""
    if "/CropBox" in page or "/Rotate" in page:
        raise UnsupportedPdfError("page crop or rotation")
    # An annotation's appearance is drawn over the page and could show other text.
    if "/Annots" in page:
        raise UnsupportedPdfError("page has annotations")
    a, b, c, d, e, f = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
    for text in matrices:  # each cm applies before the CTM so far: M x CTM
        a2, b2, c2, d2, e2, f2 = _finite(float(x) for x in text.split())
        a, b, c, d, e, f = _finite((a2 * a + b2 * c, a2 * b + b2 * d, c2 * a + d2 * c,
                                    c2 * b + d2 * d, e2 * a + f2 * c + e, e2 * b + f2 * d + f))
    box = _finite(float(x) for x in page.get("/MediaBox", []))
    if len(box) != 4:
        raise UnsupportedPdfError("page has no media box")
    x0, y0, x1, y1 = box
    tol = _PAGE_TOLERANCE
    if (abs(b) > 1e-9 or abs(c) > 1e-9 or a <= 0 or d <= 0
            or abs(e - x0) > tol or abs(f - y0) > tol
            or abs(e + a - x1) > tol or abs(f + d - y1) > tol):
        raise UnsupportedPdfError("image does not cover exactly the page")


def _invert_for(cs) -> bool:
    """Whether the image's 0 bits are white (so must be flipped for a 1-bit grey PNG)."""
    if cs == "/DeviceGray":
        return False
    if isinstance(cs, list) and len(cs) == 4 and cs[0] == "/Indexed" and cs[1] == "/DeviceRGB" \
            and int(cs[2]) == 1:
        lookup = cs[3].get_object() if hasattr(cs[3], "get_object") else cs[3]
        if hasattr(lookup, "get_data"):  # a palette stream: never decoded here
            raise UnsupportedPdfError("image colour space is not plain black and white")
        # pypdf may read a palette string back as text; its raw bytes are kept.
        lookup = getattr(lookup, "original_bytes", None) or bytes(lookup)
        if lookup == _WHITE + _BLACK:
            return True
        if lookup == _BLACK + _WHITE:
            return False
    raise UnsupportedPdfError("image colour space is not plain black and white")


def _page_image(page):
    """The page's single drawn 1-bit Flate image: (width, height, raw bytes, invert)."""
    res = page.get("/Resources")
    res = res.get_object() if res is not None else None
    if not hasattr(res, "keys") or set(res.keys()) - {"/ProcSet", "/XObject"}:
        raise UnsupportedPdfError("page resources other than one image")
    xobjs = res.get("/XObject")
    xobjs = xobjs.get_object() if xobjs is not None else None
    if not hasattr(xobjs, "keys") or len(list(xobjs.keys())) != 1:
        raise UnsupportedPdfError("page is not exactly one image")
    name = list(xobjs.keys())[0]
    m = _DRAW_ONE_IMAGE.match(_page_content(page).decode("latin-1"))
    if not m or "/" + m.group(3) != name:
        raise UnsupportedPdfError("page does not draw exactly its one image")
    _check_covers_page(page, [g for g in m.groups()[:2] if g])
    img = xobjs[name].get_object()
    filt = img.get("/Filter")
    filt = list(filt) if isinstance(filt, list) else [filt]
    if (img.get("/Subtype") != "/Image" or filt != ["/FlateDecode"]
            or int(img.get("/BitsPerComponent", 0)) != 1
            or any(k in img for k in ("/SMask", "/Mask", "/Decode", "/DecodeParms", "/ImageMask"))):
        raise UnsupportedPdfError("image is not a plain 1-bit Flate image")
    cs = img.get("/ColorSpace")
    cs = cs.get_object() if hasattr(cs, "get_object") else cs
    if isinstance(cs, list):
        cs = [c.get_object() if hasattr(c, "get_object") else c for c in cs]
    invert = _invert_for(cs)
    w, h = int(img.get("/Width", 0)), int(img.get("/Height", 0))
    if w <= 0 or h <= 0 or w * h > MAX_IMAGE_PIXELS:
        raise UnsupportedPdfError("image size out of bounds")
    raw = img._data  # the stream as stored (compressed)
    if len(raw) > MAX_STREAM_BYTES:
        raise UnsupportedPdfError("image stream too large")
    return w, h, raw, invert


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill tesseract and anything it started. On POSIX it leads its own process group
    (start_new_session), so the group goes with it; on Windows taskkill /T walks the tree."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S603,S607
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def main() -> None:
    workdir, tesseract, page_timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])
    if sys.platform.startswith("linux"):
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_ADDRESS_SPACE, _ADDRESS_SPACE))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # an over-size write fails, not kills us
    pdf_path = os.path.join(workdir, "in.pdf")
    if os.path.getsize(pdf_path) > MAX_PDF_BYTES:
        return _result(workdir, {"ok": False, "reason": "pdf too large"})
    with open(pdf_path, "rb") as fh:
        data = fh.read()
    if not data.startswith(b"%PDF-"):
        return _result(workdir, {"ok": False, "reason": "not a pdf"})

    from PIL import Image, ImageOps
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return _result(workdir, {"ok": False, "reason": "pdf encrypted"})
        pages = reader.pages
        if len(pages) > MAX_PAGES:
            return _result(workdir, {"ok": False, "reason": f"more than {MAX_PAGES} pages"})
        images = [_page_image(p) for p in pages]
    except UnsupportedPdfError as exc:
        return _result(workdir, {"ok": False, "reason": f"unsupported pdf: {exc}"})
    except Exception as exc:  # noqa: BLE001 -- any parse failure is a rejection
        return _result(workdir, {"ok": False, "reason": f"unreadable pdf ({type(exc).__name__})"})

    env = {"PATH": os.environ.get("PATH", ""), "OMP_THREAD_LIMIT": "1"}
    if os.environ.get("SYSTEMROOT"):  # Windows needs it to start any program
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    words: list = []
    for pno, (w, h, raw, invert) in enumerate(images, start=1):
        try:
            pixels = _bounded_inflate(raw, ((w + 7) // 8) * h, exact=((w + 7) // 8) * h)
        except UnsupportedPdfError as exc:
            return _result(workdir, {"ok": False, "reason": f"unsupported pdf: {exc}"})
        except zlib.error:
            return _result(workdir, {"ok": False, "reason": f"page {pno} image undecodable"})
        img = Image.frombytes("1", (w, h), pixels)
        if invert:
            img = ImageOps.invert(img.convert("L")).convert("1")
        png = os.path.join(workdir, f"p{pno}.png")
        img.save(png)
        outbase = os.path.join(workdir, f"p{pno}")
        try:
            proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
                [tesseract, png, outbase, "-l", "eng", "tsv"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, cwd=workdir, start_new_session=os.name == "posix",
            )
        except OSError:
            return _result(workdir, {"ok": False, "reason": "tesseract not found"})
        if os.name == "posix":  # the parent kills this group too, on every exit path
            with open(os.path.join(workdir, "tesseract.pgid"), "w") as fh:
                fh.write(str(proc.pid))
        try:
            proc.wait(timeout=page_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return _result(workdir, {"ok": False, "reason": f"page {pno} ocr timed out"})
        if os.name == "posix":
            _kill_tree(proc)  # tesseract exited; nothing it started may outlive the page
        tsv = outbase + ".tsv"
        if proc.returncode != 0 or not os.path.exists(tsv):
            return _result(workdir, {"ok": False, "reason": f"page {pno} ocr failed ({proc.returncode})"})
        if os.path.getsize(tsv) > MAX_TSV_BYTES:
            return _result(workdir, {"ok": False, "reason": f"page {pno} ocr output too large"})
        with open(tsv, encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()[1:]
        for row in rows:
            cols = row.split("\t")
            if len(cols) == 12 and cols[0] == "5" and cols[11].strip():
                words.append([pno, f"{pno}.{cols[2]}.{cols[3]}.{cols[4]}", cols[11], float(cols[10])])
        if len(words) > MAX_WORDS:
            return _result(workdir, {"ok": False, "reason": "ocr output too large"})
        os.remove(png)
        os.remove(tsv)
    _result(workdir, {"ok": True, "pages": len(pages), "words": words})


if __name__ == "__main__":
    main()
