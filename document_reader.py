#!/usr/local/bin/python3.11
"""
JARVIS Document Reader
─────────────────────────
Reads PDF, DOCX, TXT files and returns text content for JARVIS to summarize.
Called by file_watcher.py when new documents land in Downloads/Desktop.
Also called directly: [ACTION:READ_DOC] /path/to/file.pdf
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.document_reader")

MAX_CHARS = 8000


# ── Internal helpers ──────────────────────────────────────────────────────────

def _read_pdf(path: str) -> str:
    """Extract text from a PDF file."""
    # Try pypdf first
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(path)
        pages_text: list[str] = []
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
                pages_text.append(text)
            except Exception:
                pages_text.append("")
        return "\n".join(pages_text)
    except ImportError:
        log.debug("pypdf not installed; falling back to raw extraction")
    except Exception as exc:
        log.warning("pypdf failed on %s: %s", path, exc)

    # Raw fallback: read bytes, extract printable ASCII sequences
    try:
        with open(path, "rb") as f:
            raw = f.read()
        # Extract runs of printable characters (length >= 4)
        chunks = re.findall(rb"[\x20-\x7e]{4,}", raw)
        return "\n".join(c.decode("ascii", errors="ignore") for c in chunks)
    except Exception as exc:
        return f"[PDF read error: {exc}]"


def _read_docx(path: str) -> str:
    """Extract text from a DOCX file."""
    # Try python-docx first
    try:
        import docx  # type: ignore
        doc = docx.Document(path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs)
    except ImportError:
        log.debug("python-docx not installed; falling back to XML extraction")
    except Exception as exc:
        log.warning("python-docx failed on %s: %s", path, exc)

    # XML fallback: unzip .docx and parse word/document.xml
    try:
        import zipfile
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(path) as z:
            with z.open("word/document.xml") as f:
                tree = ET.parse(f)
        root = tree.getroot()
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        texts: list[str] = []
        for t_elem in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
            if t_elem.text:
                texts.append(t_elem.text)
        return " ".join(texts)
    except Exception as exc:
        return f"[DOCX read error: {exc}]"


def _read_text(path: str) -> str:
    """Read plain text / CSV / JSON / Markdown file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as exc:
        return f"[Text read error: {exc}]"


def _pdf_page_count(path: str) -> int:
    """Return page count for a PDF, or 0 on failure."""
    try:
        from pypdf import PdfReader  # type: ignore
        return len(PdfReader(path).pages)
    except Exception:
        return 0


# ── Public API ────────────────────────────────────────────────────────────────

def read_document(path: str) -> str:
    """Extract text from PDF, DOCX, or TXT.

    PDF:  try pypdf first, fall back to raw byte extraction
    DOCX: try python-docx, fall back to XML text extraction
    TXT/MD/CSV/JSON: direct read, truncated to 8000 chars
    Returns extracted text (max 8000 chars) or error string.
    Never raises.
    """
    try:
        p = Path(path)
        ext = p.suffix.lower()

        if ext == ".pdf":
            text = _read_pdf(path)
        elif ext in (".docx", ".doc"):
            text = _read_docx(path)
        elif ext in (".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".rst"):
            text = _read_text(path)
        else:
            # Generic fallback: try reading as text
            text = _read_text(path)

        return text[:MAX_CHARS] if len(text) > MAX_CHARS else text

    except Exception as exc:
        log.error("Unexpected error reading %s: %s", path, exc)
        return f"[Document read error: {exc}]"


def summarize_for_voice(path: str, max_sentences: int = 3) -> str:
    """Read document and return a brief description suitable for TTS.

    Does NOT call LLM — returns first N sentences + file metadata.
    Format: "filename.pdf — X pages, starts with: [first 200 chars]"
    For the full LLM summary, caller passes the result to generate_response.
    """
    try:
        p = Path(path)
        info = get_file_info(path)
        text = read_document(path)

        # Strip error strings from preview
        if text.startswith("["):
            preview = text[:200]
        else:
            # Split into sentences (rough)
            sentences = re.split(r"(?<=[.!?])\s+", text.strip())
            sentences = [s.strip() for s in sentences if s.strip()]
            preview = " ".join(sentences[:max_sentences])[:200]

        name = info.get("name", p.name)
        size_kb = info.get("size_kb", 0)
        pages = info.get("page_count", 0)

        meta_parts: list[str] = [name]
        if pages:
            meta_parts.append(f"{pages} page{'s' if pages != 1 else ''}")
        meta_parts.append(f"{size_kb:.1f} KB")

        return f"{', '.join(meta_parts)} — starts with: {preview}"

    except Exception as exc:
        return f"[Summary error: {exc}]"


def get_file_info(path: str) -> dict:
    """Return {name, size_kb, extension, page_count (PDFs), word_count} dict."""
    try:
        p = Path(path)
        stat = p.stat()
        size_kb = stat.st_size / 1024
        ext = p.suffix.lower()

        page_count = 0
        if ext == ".pdf":
            page_count = _pdf_page_count(path)

        text = read_document(path)
        word_count = len(text.split()) if not text.startswith("[") else 0

        return {
            "name": p.name,
            "size_kb": round(size_kb, 2),
            "extension": ext,
            "page_count": page_count,
            "word_count": word_count,
        }
    except Exception as exc:
        log.warning("get_file_info failed for %s: %s", path, exc)
        return {
            "name": Path(path).name,
            "size_kb": 0.0,
            "extension": Path(path).suffix.lower(),
            "page_count": 0,
            "word_count": 0,
        }
