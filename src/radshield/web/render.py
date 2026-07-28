"""PDF page rasterisation.

Rendering happens server-side so that Python remains the single authority on
page geometry: the browser receives an image plus the page size in PDF units,
and every click it reports is converted back to PDF space with a scalar.  No
coordinate logic lives in JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass


class PdfError(RuntimeError):
    """Raised when a PDF cannot be opened or rendered."""


def _fitz():
    """Import PyMuPDF lazily so the physics package stays dependency-free."""
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise PdfError(
            "PyMuPDF is required for PDF rendering. Install it with: pip install pymupdf"
        ) from exc
    return fitz


@dataclass(frozen=True)
class PageInfo:
    """Geometry of one PDF page, in PDF units (1/72 inch)."""

    page_count: int
    width: float
    height: float


def page_info(pdf_bytes: bytes, page: int = 0) -> PageInfo:
    """Return the page count and the size of ``page``."""
    fitz = _fitz()
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if not 0 <= page < doc.page_count:
                raise PdfError(f"page {page} out of range; document has {doc.page_count} pages")
            rect = doc[page].rect
            return PageInfo(page_count=doc.page_count, width=rect.width, height=rect.height)
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"could not read PDF: {exc}") from exc


def render_page(pdf_bytes: bytes, page: int = 0, zoom: float = 2.0) -> bytes:
    """Rasterise a page to PNG.

    Args:
        pdf_bytes: The PDF file content.
        page: Zero-based page index.
        zoom: Scale factor; 2.0 gives 144 dpi, which keeps architectural line
            work legible without producing unwieldy images.

    Returns:
        PNG bytes.
    """
    fitz = _fitz()
    if not 0.1 <= zoom <= 8.0:
        raise PdfError(f"zoom must be between 0.1 and 8.0, got {zoom}")
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if not 0 <= page < doc.page_count:
                raise PdfError(f"page {page} out of range; document has {doc.page_count} pages")
            pixmap = doc[page].get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            return pixmap.tobytes("png")
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"could not render PDF page: {exc}") from exc
