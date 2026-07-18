"""Binary-document → text extractors for the Globus vault ingest.

Without these, the vault silently skips every PDF, .docx, .xlsx and .pptx in a
member's Drive — which is often the majority of the real documents (contracts,
decks, spreadsheets). A member would then ask their assistant about a file
sitting in their own Drive and be told "I have no record of that", which reads
as the assistant being wrong rather than the ingest being blind.

Three of the four formats need **no dependency at all**: OOXML (.docx/.xlsx/.pptx)
is a zip of XML, so stdlib `zipfile` + `ElementTree` is enough (XLSX is handled by
`google_drive.xlsx_to_text`, which every Google-Sheet export already flows
through). Only PDF needs a library (pypdf).

`pdf_to_text` degrades cleanly if pypdf is absent — it raises `ExtractionError`
so the one file is skipped and named, rather than taking the whole Drive crawl
down with it. See ExtractionError below for why failures RAISE rather than return
a marker string.
"""
from __future__ import annotations
import io
import re
import zipfile
import xml.etree.ElementTree as ET

# OOXML namespaces.
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
PDF_MIME = "application/pdf"

# A backstop against a runaway file, not an editorial limit. The vault builder
# truncates far shorter for the LLM anyway — but the full text is what lands on
# disk and what `search_content` greps, so we keep far more than the LLM reads.
MAX_EXTRACT_CHARS = 8_000_000

# --- Hardening. These files are UNTRUSTED. ---------------------------------
#
# Everything below parses a document that arrived in a member's Drive. The
# operator does not control it: a member can save any document anyone sent them,
# and the background sync then feeds it straight to this code — inside the web
# server process, with many workers running in parallel. An OOM here is a full
# outage, so the two classic XML/zip attacks both have to be closed:
#
#  1. Decompression bomb. A download-size cap bounds the bytes on the wire, not
#     the bytes in RAM — zip ratios of 1000:1 are easy, so a few-MB .docx can
#     declare gigabytes, and zipfile.read() materializes a member in full. We
#     therefore check the archive's DECLARED uncompressed size before reading a
#     single byte. 150 MB is far above any real document and bounds the worst
#     case even with many workers simultaneously at the ceiling.
#
#  2. Entity expansion ("billion laughs"). Python's ElementTree is documented as
#     VULNERABLE to this (it is safe against external-entity fetches, not against
#     internal expansion). A legitimate OOXML part never carries a DTD, so the
#     cheapest correct defence is to refuse one outright rather than take on a
#     defusedxml dependency.
MAX_OOXML_UNCOMPRESSED = 150_000_000
# pypdf can loop or parse quadratically on malformed input, and PDF has its own
# compressed streams — so bound the work the same way the zip path is bounded.
MAX_PDF_PAGES = 5_000
_DTD_RE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.I)
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


class ExtractionError(Exception):
    """A file could not be read, or was refused as hostile.

    RAISED, never returned as text. That distinction is the whole point.

    If an extractor returned a string marker on failure — e.g.
    "[pdf skipped: pypdf not installed]" — and the caller only tests
    `if not text:`, that non-empty marker walks straight into the SUCCESS path:
    extracted=1, the marker written to disk as the document's content, fed to
    the vault builder, and liable to be shown to a member as if it were part of
    their own file. Bomb rejections would likewise never reach `skip_reason` —
    the one abuse signal we most want to be able to query.

    So: an extraction that failed must be INDISTINGUISHABLE from any other skip,
    and an empty result must mean "genuinely empty", not "something went wrong".
    Both bulk and on-demand callers already wrap each file in `except` →
    `skip_reason`, so raising keeps the degrade-don't-crash property: one hostile
    document is skipped and named, and the crawl carries on.
    """


def _q(ns, tag):
    return f"{{{ns}}}{tag}"


def open_ooxml(data):
    """Open OOXML bytes as a zip, refusing decompression bombs. Raises."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        raise ExtractionError(f"not a readable OOXML zip: {type(e).__name__}") from e
    # zi.file_size is central-directory metadata — reading it decompresses nothing.
    # An attacker cannot under-declare it either: CPython's ZipExtFile.read() uses
    # that same field as its output limit, so a small lie truncates the attack.
    declared = sum(zi.file_size for zi in zf.infolist())
    if declared > MAX_OOXML_UNCOMPRESSED:
        raise ExtractionError(
            "decompression bomb — archive declares {:,} uncompressed bytes "
            "from {:,} on disk".format(declared, len(data)))
    return zf


def parse_part(zf, name):
    """Read + parse one XML part of an OOXML archive. Raises on a DTD."""
    raw = zf.read(name)
    # A DOCTYPE must appear in the prolog, before the root element; scanning the
    # first 1 MB is generous and keeps this O(1) rather than O(file).
    if _DTD_RE.search(raw[:1_000_000]):
        raise ExtractionError(
            "XML part declares a DTD/ENTITY — refusing (OOXML never legitimately does)")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as e:
        raise ExtractionError(f"malformed XML in {name}: {e}") from e


def docx_to_text(data, max_chars=MAX_EXTRACT_CHARS):
    """.docx bytes → plain text, stdlib only.

    A .docx is a zip; the body is word/document.xml. Text lives in <w:t> runs,
    grouped into <w:p> paragraphs. We emit one line per paragraph so tables and
    lists keep their row structure instead of collapsing into one blob.
    """
    zf = open_ooxml(data)
    try:
        root = parse_part(zf, "word/document.xml")
    except KeyError:
        raise ExtractionError("docx has no word/document.xml") from None

    lines, running = [], 0
    for para in root.iter(_q(_NS_W, "p")):
        parts = []
        for node in para.iter():
            tag = node.tag
            if tag == _q(_NS_W, "t"):
                parts.append(node.text or "")
            elif tag == _q(_NS_W, "tab"):
                parts.append("\t")
            elif tag in (_q(_NS_W, "br"), _q(_NS_W, "cr")):
                parts.append("\n")
        line = "".join(parts).strip()
        if not line:
            continue
        lines.append(line)
        running += len(line) + 1
        if running >= max_chars:
            lines.append("\n[... truncated at {:,} chars ...]".format(max_chars))
            break
    return "\n".join(lines)


def pptx_to_text(data, max_chars=MAX_EXTRACT_CHARS):
    """.pptx bytes → plain text, stdlib only.

    Slides are ppt/slides/slideN.xml; text lives in DrawingML <a:t> runs. Slides
    are emitted in numeric order (zip order is lexicographic, so slide10 would
    otherwise sort before slide2) under `# Slide N` headers, mirroring the
    `# Tab:` convention xlsx_to_text already uses.
    """
    zf = open_ooxml(data)
    slides = []
    for name in zf.namelist():
        m = _SLIDE_RE.match(name)
        if m:
            slides.append((int(m.group(1)), name))
    slides.sort()

    out, running = [], 0
    for num, name in slides:
        try:
            root = parse_part(zf, name)
        except Exception:
            continue
        texts = [(t.text or "") for t in root.iter(_q(_NS_A, "t"))]
        body = "\n".join(t for t in (s.strip() for s in texts) if t)
        if not body:
            continue
        chunk = f"\n\n# Slide {num}\n{body}"
        out.append(chunk)
        running += len(chunk)
        if running >= max_chars:
            out.append("\n[... truncated at {:,} chars ...]".format(max_chars))
            break
    return "".join(out).strip()


def pdf_available():
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_to_text(data, max_chars=MAX_EXTRACT_CHARS, max_pages=MAX_PDF_PAGES):
    """PDF bytes → plain text via pypdf (the one extractor that needs a dep).

    Raises ExtractionError when the PDF cannot be read, so the failure lands in
    skip_reason instead of being written to disk as the document's own text.

    A scanned / image-only PDF legitimately has no text layer and returns "" —
    that is not an error, and it correctly records as "extracted empty". Reading
    it would need OCR, which we do not do.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractionError("pypdf not installed on this host") from None

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise ExtractionError(f"unreadable PDF: {type(e).__name__}: {e}") from e

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")          # many PDFs are "encrypted" with an empty owner pw
        except Exception:
            raise ExtractionError("PDF is encrypted") from None

    out, running = [], 0
    for i, page in enumerate(reader.pages, 1):
        # Bound the work: PDF carries its own compressed streams, and pypdf has a
        # history of looping or going quadratic on malformed input. Same reasoning
        # as MAX_OOXML_UNCOMPRESSED — this runs in the web server, many at a time.
        if i > max_pages:
            out.append(f"\n\n[... truncated at {max_pages:,} pages ...]")
            break
        try:
            text = page.extract_text() or ""
        except Exception:
            continue                     # one broken page must not lose the rest
        text = text.strip()
        if not text:
            continue
        chunk = f"\n\n# Page {i}\n{text}"
        out.append(chunk)
        running += len(chunk)
        if running >= max_chars:
            out.append("\n[... truncated at {:,} chars ...]".format(max_chars))
            break
    return "".join(out).strip()
