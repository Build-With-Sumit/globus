"""Google Drive API + content extractors.

Stdlib only. Three responsibilities:
  1. Drive API client (list/export/download) — paginates the v3 endpoints
     up to the hard ceilings.
  2. Content classification + extraction — Google Docs export as markdown,
     Sheets export as XLSX then flatten via xlsx_to_text (preserves all
     tabs, unlike CSV export which drops everything past the first).
  3. Disk persistence — `write_extracted_file` writes one-file-per-doc
     under `RAW_DATA_DIR/email/account/source/external_id.ext`.

`vault_files_upsert` is the single index-write point — both keepers and
skips upsert a row so the dashboard reflects everything Drive listed.

DB-touching but doesn't depend on oauth_db; sync_drive.py wires them.
"""
from __future__ import annotations
import base64
import io
import json
import os
import re
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from db_helpers import db_write
from drive_extract import (
    DOCX_MIME, XLSX_MIME, PPTX_MIME, PDF_MIME,
    docx_to_text, pptx_to_text, pdf_to_text,
)


# ─────────────────────────────────────────────────────────────────────
# Constants — sized for "max out the subscription" sync, but with hard
# ceilings so a 500K-file Drive can't DoS the worker.
# ─────────────────────────────────────────────────────────────────────

GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"

GOOGLE_DRIVE_MAX_FILES = 10_000           # hard ceiling per sync
GOOGLE_DRIVE_PAGE_SIZE = 1000             # Drive API list page max
GOOGLE_DRIVE_MAX_FILE_BYTES = 5_000_000   # 5 MB per-file download cap
GOOGLE_DRIVE_LOOKBACK_DAYS = 1825         # ~5 years
GOOGLE_DRIVE_XLSX_EXPORT_MAX_BYTES = 25_000_000   # sheet exports balloon
GOOGLE_SHEET_EXTRACT_MAX_CHARS = 8_000_000        # flattened-text cap

# Aggregated chat-fallback (`globus_vault_sources` row) — top-N most-recent
# files concatenated for the no-digest path. Keeps the chat working from
# day 1 even before any per-file `read_file` lookups happen.
GOOGLE_DRIVE_AGG_FILES = 100
GOOGLE_DRIVE_AGG_PER_FILE = 5_000

# Where extracted text gets written, one-file-per-document.
# Per-member-isolated via path: {RAW}/{email}/{account}/{source}/{id}.{ext}
RAW_DATA_DIR = os.environ.get("GLOBUS_RAW_DATA_DIR", "/var/lib/globus/raw-data")

# Mime → (extract_method, output_ext, export_mime_if_doc)
DRIVE_EXTRACTABLE = {
    "application/vnd.google-apps.document":     ("export", "md",  "text/markdown"),
    "application/vnd.google-apps.spreadsheet":  ("export", "txt", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "application/vnd.google-apps.presentation": ("export", "txt", "text/plain"),
    "application/vnd.google-apps.drawing":      ("export", "svg", "image/svg+xml"),
    "application/vnd.google-apps.script":       ("export", "json", "application/vnd.google-apps.script+json"),
    "text/markdown":  ("download", "md",  None),
    "text/plain":     ("download", "txt", None),
    "text/html":      ("download", "html", None),
    "text/csv":       ("download", "csv", None),
    "text/xml":       ("download", "xml", None),
    "application/json": ("download", "json", None),
    "application/xml":  ("download", "xml", None),
    "application/rtf":  ("download", "rtf", None),
    # Binary office documents + PDF — downloaded whole, then turned into text by
    # drive_extract. OOXML (.docx/.xlsx/.pptx) is stdlib-only; PDF needs the
    # optional `pypdf` dep and degrades to a named skip if it is absent.
    DOCX_MIME: ("download", "txt", None),
    XLSX_MIME: ("download", "txt", None),
    PPTX_MIME: ("download", "txt", None),
    PDF_MIME:  ("download", "txt", None),
}

# Folders, shortcuts, forms, sites, images, video, audio, archives — skip.
DRIVE_SKIP_MIME_PREFIXES = (
    "image/", "video/", "audio/",
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.shortcut",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.photo",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.fusiontable",
    "application/octet-stream",
    "application/zip", "application/x-zip", "application/x-rar",
    "application/x-tar", "application/x-gzip", "application/x-bzip2",
    "application/x-7z-compressed",
    "application/x-msdownload", "application/x-executable",
)


# ─────────────────────────────────────────────────────────────────────
# Disk helpers — one-file-per-doc, per-member-isolated path
# ─────────────────────────────────────────────────────────────────────

def _raw_data_path(email, provider_account, source_type, external_id, ext):
    """Build the per-file disk path; create parent dirs as needed. The
    email-in-path is the filesystem-level per-member isolation backstop."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(external_id))[:200]
    safe_pa = re.sub(r"[^A-Za-z0-9@_.-]", "_", str(provider_account or "_"))[:200]
    safe_em = re.sub(r"[^A-Za-z0-9@_.-]", "_", str(email))[:200]
    safe_ext = re.sub(r"[^A-Za-z0-9]", "", str(ext or "bin"))[:8] or "bin"
    dir_path = os.path.join(RAW_DATA_DIR, safe_em, safe_pa, source_type)
    os.makedirs(dir_path, exist_ok=True)
    return os.path.join(dir_path, f"{safe_id}.{safe_ext}")


def write_extracted_file(email, provider_account, source_type, external_id,
                          ext, content):
    """Write extracted text content to disk. Returns (path, byte_length).
    Caller is responsible for indexing via vault_files_upsert."""
    path = _raw_data_path(email, provider_account, source_type, external_id, ext)
    data = content.encode("utf-8", errors="replace") if isinstance(content, str) else content
    with open(path, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path, len(data)


def _parse_iso_dt(s):
    """Best-effort ISO-8601 → naive UTC datetime. Returns None on failure.
    PyMySQL can serialize datetime objects to MySQL TIMESTAMP, but it CANNOT
    serialize raw ISO strings like '2026-06-19T10:30:00.000Z' (MySQL strict
    mode rejects with error 1292)."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s.replace(tzinfo=None) if s.tzinfo else s
    try:
        s2 = str(s).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────
# vault_files upsert — single index-write point
# ─────────────────────────────────────────────────────────────────────

def vault_files_upsert(email, connection_id, provider_account, source_type,
                        external_id, *, filename=None, mime_type=None,
                        size_bytes=None, modified_at=None,
                        extracted_path=None, extracted_chars=None,
                        skip_reason=None, metadata=None):
    """Upsert one file index row. Email-scoped (per-member isolation).
    Both keepers (extracted_path set) and skips (skip_reason set) upsert
    here so the dashboard reflects every file Drive listed."""
    extracted = 1 if extracted_path else 0
    modified_dt = _parse_iso_dt(modified_at)
    db_write(
        "INSERT INTO globus_vault_files "
        "(email, connection_id, provider_account, source_type, external_id, "
        " filename, mime_type, size_bytes, modified_at, extracted, "
        " extracted_path, extracted_chars, skip_reason, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "  connection_id=VALUES(connection_id), "
        "  provider_account=VALUES(provider_account), "
        "  filename=VALUES(filename), mime_type=VALUES(mime_type), "
        "  size_bytes=VALUES(size_bytes), modified_at=VALUES(modified_at), "
        "  extracted=VALUES(extracted), extracted_path=VALUES(extracted_path), "
        "  extracted_chars=VALUES(extracted_chars), "
        "  skip_reason=VALUES(skip_reason), metadata=VALUES(metadata), "
        "  updated_at=NOW()",
        (email, connection_id, provider_account, source_type, external_id,
         filename, mime_type, size_bytes, modified_dt, extracted,
         extracted_path, extracted_chars, skip_reason,
         json.dumps(metadata) if metadata else None))


# ─────────────────────────────────────────────────────────────────────
# Drive API client (paginated)
# ─────────────────────────────────────────────────────────────────────

def drive_list_files(access_token, query, max_results=GOOGLE_DRIVE_MAX_FILES,
                      page_size=GOOGLE_DRIVE_PAGE_SIZE):
    """Page through Drive search results up to max_results total. Drive's
    pageSize caps at 1000; for >1000 we paginate via nextPageToken."""
    base_fields = "files(id,name,mimeType,modifiedTime,parents,size,owners,webViewLink)"
    fields = "nextPageToken," + base_fields
    out = []
    page_token = None
    while len(out) < max_results:
        remaining = max_results - len(out)
        size = min(page_size, remaining)
        url = (f"{GOOGLE_DRIVE_API}/files?q={urllib.parse.quote(query)}"
               f"&pageSize={size}&fields={urllib.parse.quote(fields)}"
               f"&supportsAllDrives=true&includeItemsFromAllDrives=true"
               f"&orderBy=modifiedTime%20desc")
        if page_token:
            url += f"&pageToken={urllib.parse.quote(page_token)}"
        req = Request(url, headers={"Authorization": "Bearer " + access_token})
        with urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode())
        out.extend(data.get("files") or [])
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def drive_export_with_mime(access_token, file_id, export_mime, max_bytes=None):
    """Export a Google-native doc as the given mime; returns bytes (capped).
    Pass GOOGLE_DRIVE_XLSX_EXPORT_MAX_BYTES (25 MB) for sheet exports —
    a 4 KB sheet can expand to 9+ MB once styles and many tabs are baked in."""
    if max_bytes is None:
        max_bytes = GOOGLE_DRIVE_MAX_FILE_BYTES
    url = (f"{GOOGLE_DRIVE_API}/files/{file_id}/export"
           f"?mimeType={urllib.parse.quote(export_mime)}")
    req = Request(url, headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=60) as r:
        return r.read(max_bytes)


def drive_export_doc(access_token, file_id, export_mime="text/markdown"):
    """Convenience: export and decode as utf-8 in one call."""
    raw = drive_export_with_mime(access_token, file_id, export_mime)
    return raw.decode("utf-8", errors="replace")


def drive_download_file(access_token, file_id):
    """Download a non-Google-native file (text/csv, text/markdown, etc.).
    Capped at GOOGLE_DRIVE_MAX_FILE_BYTES."""
    url = f"{GOOGLE_DRIVE_API}/files/{file_id}?alt=media"
    req = Request(url, headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=60) as r:
        return r.read(GOOGLE_DRIVE_MAX_FILE_BYTES)


def drive_classify(f):
    """Return (extract_method, output_ext, export_mime_or_None,
    skip_reason_or_None). extract_method is 'export'|'download'|None;
    skip_reason is set iff method is None."""
    mime = (f.get("mimeType") or "").lower()
    try:
        size = int(f.get("size") or 0)
    except (ValueError, TypeError):
        size = 0
    if size and size > GOOGLE_DRIVE_MAX_FILE_BYTES:
        return None, None, None, f"too large ({size:,} bytes)"
    for pref in DRIVE_SKIP_MIME_PREFIXES:
        if mime.startswith(pref):
            return None, None, None, f"skipped mime {mime}"
    if mime in DRIVE_EXTRACTABLE:
        method, ext, export_mime = DRIVE_EXTRACTABLE[mime]
        return method, ext, export_mime, None
    return None, None, None, f"unsupported mime {mime}"


# ─────────────────────────────────────────────────────────────────────
# XLSX flattener — preserves every tab, unlike Drive's CSV export
# (CSV export drops everything past the first sheet, which silently
# loses ~60% of the content of multi-tab trackers / P&Ls / fundings).
# ─────────────────────────────────────────────────────────────────────

_XLSX_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_NS_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XLSX_NS_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_XLSX_COL_RE = re.compile(r"^([A-Z]+)\d+$")


def _xlsx_col_to_index(col_letters):
    n = 0
    for ch in col_letters:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n - 1


def xlsx_to_text(data, max_chars=GOOGLE_SHEET_EXTRACT_MAX_CHARS):
    """Parse XLSX bytes and emit plain text of EVERY sheet, concatenated
    with `# Tab: <name>` headers. Stdlib only (zipfile + ElementTree)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, Exception) as e:
        return f"[xlsx parse error: {type(e).__name__}: {e}]"

    main_q = lambda tag: f"{{{_XLSX_NS_MAIN}}}{tag}"
    rels_q = lambda tag: f"{{{_XLSX_NS_RELS}}}{tag}"
    pkg_q = lambda tag: f"{{{_XLSX_NS_PKG_RELS}}}{tag}"

    try:
        shared = []
        try:
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(main_q("si")):
                shared.append("".join(
                    (t.text or "") for t in si.iter(main_q("t"))))
        except KeyError:
            pass

        wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet_defs = []
        for s in wb_root.iter(main_q("sheet")):
            sheet_defs.append((s.get("name") or "Sheet",
                               s.get(rels_q("id"))))

        rel_map = {}
        try:
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for r in rels_root.iter(pkg_q("Relationship")):
                rel_map[r.get("Id")] = r.get("Target") or ""
        except KeyError:
            pass

        out = []
        running = 0
        for sheet_name, rid in sheet_defs:
            target = rel_map.get(rid) or ""
            if not target:
                continue
            sheet_path = ("xl/" + target.lstrip("/")
                          if not target.startswith("xl/") else target)
            try:
                sheet_xml = zf.read(sheet_path)
            except KeyError:
                continue
            sheet_root = ET.fromstring(sheet_xml)

            header = f"\n\n# Tab: {sheet_name}\n"
            out.append(header)
            running += len(header)

            for row in sheet_root.iter(main_q("row")):
                cells_by_col = {}
                max_col = -1
                for c in row.findall(main_q("c")):
                    ref = c.get("r") or ""
                    m = _XLSX_COL_RE.match(ref)
                    col_idx = (_xlsx_col_to_index(m.group(1)) if m
                               else len(cells_by_col))
                    t = c.get("t") or "n"
                    val = ""
                    if t == "s":
                        v = c.find(main_q("v"))
                        if v is not None and v.text is not None:
                            try:
                                val = shared[int(v.text)]
                            except (ValueError, IndexError):
                                val = ""
                    elif t == "inlineStr":
                        is_e = c.find(main_q("is"))
                        if is_e is not None:
                            val = "".join((t.text or "")
                                          for t in is_e.iter(main_q("t")))
                    elif t == "str":
                        v = c.find(main_q("v"))
                        val = (v.text if v is not None else "") or ""
                    else:
                        v = c.find(main_q("v"))
                        val = (v.text if v is not None else "") or ""
                    val = val.replace("\r", " ").replace("\n", " ").strip()
                    cells_by_col[col_idx] = val
                    if col_idx > max_col:
                        max_col = col_idx
                if max_col < 0:
                    continue
                row_cells = [cells_by_col.get(i, "") for i in range(max_col + 1)]
                while row_cells and not row_cells[-1].strip():
                    row_cells.pop()
                if not row_cells:
                    continue
                line = "\t".join(row_cells) + "\n"
                out.append(line)
                running += len(line)
                if running > max_chars:
                    out.append(f"\n... (truncated at {max_chars:,} chars)\n")
                    return "".join(out).strip()
        return "".join(out).strip()
    finally:
        zf.close()


# ─────────────────────────────────────────────────────────────────────
# Shared extraction dispatch — turns exported/downloaded bytes into text.
# Both the bulk sync (sync_drive._process) and the on-demand read_file path
# call this so they can never drift on which mimes are understood.
# ─────────────────────────────────────────────────────────────────────

def extract_downloaded_text(mime, raw):
    """Exported/downloaded bytes → text, dispatched by mime.

    Raises ExtractionError (from drive_extract) on a hostile/broken binary —
    never returns a marker string, so a failure records as a skip rather than
    being written to disk as the document's own content.

    Google Sheets export to XLSX bytes, so the native-spreadsheet mime and a
    real .xlsx both flow through xlsx_to_text."""
    m = (mime or "").lower()
    if m == "application/vnd.google-apps.spreadsheet" or m == XLSX_MIME:
        return xlsx_to_text(raw)
    if m == DOCX_MIME:
        return docx_to_text(raw)
    if m == PPTX_MIME:
        return pptx_to_text(raw)
    if m == PDF_MIME or m.startswith("application/pdf"):
        return pdf_to_text(raw)
    return (raw.decode("utf-8", errors="replace")
            if isinstance(raw, (bytes, bytearray)) else str(raw))


# ─────────────────────────────────────────────────────────────────────
# One-shot extract — used by on-demand `read_file` path in the orchestrator
# ─────────────────────────────────────────────────────────────────────

def drive_extract_one(access_token, f):
    """Download + extract a single Drive file `f` (as returned by
    drive_list_files). Returns (text, ext) or (None, skip_reason).
    Does NOT touch the index — caller decides whether to cache + upsert."""
    method, ext, export_mime, skip_reason = drive_classify(f)
    if method is None:
        return None, skip_reason
    fid = f["id"]
    mime = (f.get("mimeType") or "").lower()
    try:
        if method == "export":
            is_sheet = mime == "application/vnd.google-apps.spreadsheet"
            raw = drive_export_with_mime(
                access_token, fid, export_mime,
                max_bytes=(GOOGLE_DRIVE_XLSX_EXPORT_MAX_BYTES
                           if is_sheet else None))
        else:
            raw = drive_download_file(access_token, fid)
        text = extract_downloaded_text(mime, raw)
        return (text or "").strip(), ext
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
