"""Lightweight text extraction for email attachments.

Covers the realistic enterprise attachment types (PDF, Word, Excel, plain text
formats, and ZIP archives) so the agent can read or summarize an attachment's
content. Binary or unsupported types return a short note instead of bytes. Pure
best-effort: any extractor error degrades to a note, never raises.
"""

import io
import zipfile
from typing import List

MAX_TEXT_CHARS = 12000
_MAX_PDF_PAGES = 50
_MAX_XLSX_ROWS = 200

# ZIP limits. An archive is the one attachment type whose decompressed size is
# not bounded by the 3MB download cap the caller already enforces, so the guard
# has to be here: a few hundred KB of zip can expand to gigabytes.
_MAX_ZIP_ENTRIES = 200
_MAX_ZIP_TEXT_MEMBERS = 10
_MAX_ZIP_MEMBER_BYTES = 5 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 20 * 1024 * 1024

_TEXT_EXTS = (
    ".txt",
    ".csv",
    ".md",
    ".json",
    ".html",
    ".htm",
    ".xml",
    ".log",
    ".yaml",
    ".yml",
)
# Office formats are ZIP containers. They are matched before the archive branch
# so a .docx is read as a document, never listed as an archive of its own XML.
_OFFICE_EXTS = (".docx", ".xlsx", ".pptx")


def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        return text[:MAX_TEXT_CHARS] + f"\n...[truncated; {len(text)} chars total]"
    return text


def _extract_pdf(content_bytes: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(content_bytes))
    pages = []
    for i, page in enumerate(reader.pages):
        if i >= _MAX_PDF_PAGES:
            pages.append("...[more pages]")
            break
        pages.append(page.extract_text() or "")
    return _truncate("\n".join(pages)) or "(no extractable text in PDF)"


def _extract_docx(content_bytes: bytes) -> str:
    import docx

    doc = docx.Document(io.BytesIO(content_bytes))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(c.text for c in row.cells))
    return _truncate("\n".join(parts)) or "(empty Word document)"


def _extract_xlsx(content_bytes: bytes) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"# Sheet: {ws.title}")
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= _MAX_XLSX_ROWS:
                out.append("...[more rows]")
                break
            out.append(", ".join("" if c is None else str(c) for c in row))
    return _truncate("\n".join(out)) or "(empty spreadsheet)"


def _zip_members(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """Real files in the archive, in listing order.

    Directories carry no content, and ``__MACOSX`` holds the resource forks a
    Mac adds when zipping from Finder. Listing either of them tells the agent
    nothing and pushes the real files past the entry cap.
    """
    members = []
    for zi in zf.infolist():
        if zi.is_dir() or zi.filename.startswith("__MACOSX/"):
            continue
        if zi.filename.rsplit("/", 1)[-1].startswith("."):
            continue
        members.append(zi)
        if len(members) >= _MAX_ZIP_ENTRIES:
            break
    return members


def _extract_zip(filename: str, content_bytes: bytes) -> str:
    """List an archive, then read the text out of as many members as the caps allow.

    The listing alone is worth returning: "what is in this zip" is a question the
    agent could not answer at all before, and a manifest is enough to decide
    whether the archive is even the right one. Reading the members is the bonus.

    Nested archives are listed but never opened. Recursing is how a zip bomb
    turns a 3MB attachment into an out-of-memory kill, and a zip inside a zip is
    rare enough in mail that refusing it costs nothing.
    """
    with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
        members = _zip_members(zf)
        if not members:
            return f"(archive '{filename}' is empty)"

        lines = [f"# Archive: {filename} ({len(members)} file(s))"]
        for zi in members:
            lines.append(f"- {zi.filename} ({zi.file_size:,} bytes)")

        budget = _MAX_ZIP_TOTAL_BYTES
        read = 0
        for zi in members:
            if read >= _MAX_ZIP_TEXT_MEMBERS:
                lines.append(f"\n...[{len(members) - read} more file(s) not read]")
                break
            name = zi.filename.lower()
            if name.endswith(".zip"):
                lines.append(f"\n## {zi.filename}\n(nested archive; not opened)")
                continue
            if not name.endswith(_TEXT_EXTS + _OFFICE_EXTS + (".pdf",)):
                continue
            if zi.file_size > _MAX_ZIP_MEMBER_BYTES or zi.file_size > budget:
                lines.append(f"\n## {zi.filename}\n(too large to read: {zi.file_size:,} bytes)")
                continue
            budget -= zi.file_size
            read += 1
            inner = extract_text(zi.filename, zf.read(zi), _depth=1)
            lines.append(f"\n## {zi.filename}\n{inner}")

        return _truncate("\n".join(lines))


def extract_text(
    filename: str, content_bytes: bytes, content_type: str = "", _depth: int = 0
) -> str:
    """Extract readable text from attachment bytes. Best-effort, never raises.

    ``_depth`` is internal: it is 1 for a file read out of an archive, which is
    what stops archive extraction recursing.
    """
    name = (filename or "").lower()
    ct = (content_type or "").lower()
    try:
        if name.endswith(".pdf") or "pdf" in ct:
            return _extract_pdf(content_bytes)

        if name.endswith(".docx") or "wordprocessingml" in ct:
            return _extract_docx(content_bytes)

        if name.endswith(".xlsx") or "spreadsheetml" in ct:
            return _extract_xlsx(content_bytes)

        if name.endswith(_TEXT_EXTS) or ct.startswith("text/") or "json" in ct or "xml" in ct:
            return _truncate(content_bytes.decode("utf-8", errors="replace"))

        # Archives last: every Office format above is also a ZIP, so this branch
        # must only see what none of them claimed.
        if _depth == 0 and (name.endswith(".zip") or "zip" in ct or "compressed" in ct):
            return _extract_zip(filename, content_bytes)
    except zipfile.BadZipFile:
        return f"(attachment '{filename}' is not a readable archive)"
    except Exception as exc:  # noqa: BLE001 - extraction is best-effort
        return f"(could not extract text from '{filename}': {type(exc).__name__})"

    return f"(attachment '{filename}' is type '{content_type or 'unknown'}'; not text-extractable)"
