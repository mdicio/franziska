"""Attachments and files to text, locally, cheapest first (CHANGELOG §5a):

1. a text layer: `pdftotext` (apt: poppler-utils), plain text;
2. OCR on CPU: `pdftoppm` renders scanned pages, `tesseract -l ita+eng` reads them and photos (apt:
   tesseract-ocr-ita). No GPU, so it also runs on the box;
3. the local model looking at the page: not yet. llama-server needs gemma-4's `--mmproj` (evals/reports/2026-10-29/queue-results.md).

A PEC envelope (`.eml`, or the `postacert.eml` inside one) is opened with the stdlib parser: its text and
readable attachments become the document's text. Office files: a `.docx` is its paragraphs and tables (zip + XML,
stdlib); an `.xlsx` each sheet's non-empty rows (openpyxl, values not formulas). Legacy `.doc`/`.xls` are not read.
"""

from __future__ import annotations

import base64
import email
import email.policy
import io
import logging
import subprocess
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree

log = logging.getLogger(__name__)

PAGES = 5  # OCR at most this many pages of a scan (several seconds each on the CPU)
TEXT_PAGES = 300  # of a PDF with a text layer (pdftotext is instant); agents read it by page from the library
MIN_TEXT = 40  # chars: a text layer shorter than this is a scan (a page number, a stamp), so OCR it
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff")
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_ROWS = 500  # per sheet: the agent gets a few thousand chars anyway (core.DOCUMENT_CHARS)


class ToolMissing(RuntimeError):
    """A reader this machine lacks (pdftotext, pdftoppm, tesseract). Raised, never turned into '': a missing tool is
    not an unreadable document. The message says what to install."""


INSTALL = {"pdftotext": "poppler-utils", "pdftoppm": "poppler-utils", "tesseract": "tesseract-ocr tesseract-ocr-ita"}


def text_of(filename: str, mime: str, data: bytes) -> str:
    """The file's text, or '' when it has none we can read. Raises ToolMissing when a reader isn't installed."""
    name = filename.lower()
    if mime == "application/pdf" or name.endswith(".pdf"):
        text = pdf_text(data)
        return text if len(text) >= MIN_TEXT else (ocr_pdf(data) or text)
    if mime.startswith("image/") or name.endswith(IMAGE_EXT):
        return ocr_image(data, Path(name).suffix or ".jpg")
    if mime == "message/rfc822" or name.endswith(".eml"):
        return eml_text(data)
    if mime == DOCX_MIME or name.endswith(".docx"):
        return docx_text(data)
    if mime == XLSX_MIME or name.endswith((".xlsx", ".xlsm")):
        return xlsx_text(data)
    if mime.startswith("text/") or name.endswith(".txt"):
        return data.decode("utf-8", "replace")
    return ""


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str:
    """A Word file's body: one line per paragraph, a table row as its cells joined by ' | '."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            body = ElementTree.fromstring(z.read("word/document.xml")).find(f"{W}body")
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as e:
        log.warning("unreadable .docx: %s", e)
        return ""

    def para(p) -> str:
        out = []
        for el in p.iter():
            if el.tag == f"{W}t":
                out.append(el.text or "")
            elif el.tag == f"{W}tab":
                out.append("\t")
            elif el.tag in (f"{W}br", f"{W}cr"):
                out.append("\n")
        return "".join(out).strip()

    lines = []
    for block in body if body is not None else ():
        if block.tag == f"{W}p":
            lines.append(para(block))
        elif block.tag == f"{W}tbl":
            for row in block.iter(f"{W}tr"):
                cells = [" ".join(filter(None, map(para, tc.iter(f"{W}p")))) for tc in row.iter(f"{W}tc")]
                if any(cells):
                    lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def xlsx_text(data: bytes) -> str:
    """A spreadsheet's sheets: a '## name' line, then each non-empty row's cells joined by ' | ' (dates as ISO)."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # zip, XML or openpyxl's own errors: not readable
        log.warning("unreadable .xlsx: %s", e)
        return ""

    def cell(v) -> str:
        if isinstance(v, datetime):
            return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat(sep=" ", timespec="minutes")
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return "" if v is None else str(v).strip()

    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for values in ws.iter_rows(values_only=True):
                cells = [cell(v) for v in values]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    rows.append(" | ".join(cells))
                if len(rows) >= SHEET_ROWS:
                    rows.append("(more rows not shown)")
                    break
            if rows:
                out += [f"## {ws.title}", *rows, ""]
    finally:
        wb.close()
    return "\n".join(out).strip()


def _run(cmd: list[str], timeout: int = 60) -> bytes | None:
    """The command's output; None when it failed on this file (a damaged PDF, a timeout)."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True).stdout
    except FileNotFoundError:
        raise ToolMissing(f"{cmd[0]} is not installed on this machine, so the document could not be read "
                          f"(apt install {INSTALL.get(cmd[0], cmd[0])})") from None
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s failed: %s", cmd[0], e)
        return None


def pdf_text(data: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "doc.pdf"
        path.write_bytes(data)
        out = _run(["pdftotext", "-layout", "-l", str(TEXT_PAGES), str(path), "-"], timeout=30)
    return out.decode("utf-8", "replace").strip() if out else ""


def ocr_pdf(data: bytes) -> str:
    """Scanned PDF: render the first pages at 300 dpi, OCR each, pages separated by a form feed."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "doc.pdf"
        path.write_bytes(data)
        if _run(["pdftoppm", "-r", "300", "-l", str(PAGES), "-png", str(path), str(Path(tmp) / "p")]) is None:
            return ""
        pages = [_tesseract(p) for p in sorted(Path(tmp).glob("p-*.png"))]
    return "\f".join(p for p in pages if p).strip()


def ocr_image(data: bytes, suffix: str = ".jpg") -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"img{suffix}"
        path.write_bytes(data)
        return _tesseract(path)


def _tesseract(path: Path) -> str:
    out = _run(["tesseract", str(path), "-", "-l", "ita+eng", "--psm", "3"], timeout=120)
    return out.decode("utf-8", "replace").strip() if out else ""


def eml_text(data: bytes) -> str:
    """A mail file: its text body plus readable attachments; a PEC's inner `postacert.eml` is the real message."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    parts = [f"From: {msg['from']}\nSubject: {msg['subject']}\nDate: {msg['date']}"]
    body = msg.get_body(("plain", "html"))
    if body is not None:
        parts.append(body.get_content())
    for att in msg.iter_attachments():
        name = att.get_filename() or ""
        if name.lower() in ("daticert.xml", "smime.p7s"):  # PEC receipts and signatures: no content
            continue
        inner = att.get_payload()
        # message/rfc822 is usually parsed inline (a list of one message); some senders base64 it instead
        if not isinstance(inner, list):
            payload = att.get_payload(decode=True)
        elif (att.get("content-transfer-encoding") or "").lower() == "base64":  # the parser read it as a body
            payload = base64.b64decode(inner[0].get_payload())
        else:
            payload = inner[0].as_bytes()
        if payload and (text := text_of(name, att.get_content_type(), payload)):
            parts.append(f"[{name or att.get_content_type()}]\n{text}")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
