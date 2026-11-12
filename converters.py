"""Convert images, spreadsheets and text into PDFs without native dependencies.

Everything here runs in pure Python (Pillow + fpdf2 + openpyxl) so it works on
Vercel Functions, where LibreOffice and friends are not available.
"""

import csv
import io
import os

from fpdf import FPDF
from PIL import Image, ImageOps

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
LATIN = "DejaVu"
LATIN_BOLD = "DejaVu-Bold"
DEVANAGARI = "NotoDeva"

MARGIN = 12
MAX_CELL_CHARS = 200

_FALLBACKS = []
_HAS_LATIN = False


class ConversionError(Exception):
    """Raised when an uploaded file cannot be turned into a PDF."""


def _new_pdf(orientation="P"):
    global _HAS_LATIN, _FALLBACKS

    pdf = FPDF(orientation=orientation, unit="mm", format="A4")

    latin = os.path.join(FONT_DIR, "DejaVuSans.ttf")
    bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
    devanagari = os.path.join(FONT_DIR, "NotoSansDevanagari-Regular.ttf")
    devanagari_bold = os.path.join(FONT_DIR, "NotoSansDevanagari-Bold.ttf")

    if not os.path.exists(latin):
        pdf.set_font("Helvetica", size=11)
        return pdf, False

    pdf.add_font(LATIN, "", latin)
    if os.path.exists(bold):
        pdf.add_font(LATIN, "B", bold)

    fallbacks = []
    if os.path.exists(devanagari):
        pdf.add_font(DEVANAGARI, "", devanagari)
        if os.path.exists(devanagari_bold):
            pdf.add_font(DEVANAGARI, "B", devanagari_bold)
        fallbacks.append(DEVANAGARI)

    # Complex scripts (Devanagari) need shaping; ignored if uharfbuzz is absent.
    try:
        pdf.set_text_shaping(True)
    except Exception:
        pass

    pdf.set_auto_page_break(auto=True, margin=MARGIN)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)

    _HAS_LATIN = True
    _FALLBACKS = [LATIN] + fallbacks
    return pdf, True


def _body_font(pdf, style="", size=11):
    """Select a font and let Devanagari fall back to the Noto font."""
    pdf.set_font(LATIN if _HAS_LATIN else "Helvetica", style if _HAS_LATIN else "", size)
    if _FALLBACKS:
        pdf.set_fallback_fonts(_FALLBACKS)


def images_to_pdf(streams):
    """Turn a list of (filename, stream) images into one multi-page PDF."""
    pdf, has_font = _new_pdf()
    pages = 0

    for name, stream in streams:
        try:
            with Image.open(stream) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode in ("RGBA", "LA", "P"):
                    rgba = image.convert("RGBA")
                    flat = Image.new("RGB", rgba.size, (255, 255, 255))
                    flat.paste(rgba, mask=rgba.split()[-1])
                    image = flat
                else:
                    image = image.convert("RGB")

                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=88)
                buffer.seek(0)

                width, height = image.size
        except Exception as exc:
            raise ConversionError(f"{name} is not a supported image") from exc

        pdf.add_page()
        usable_w = pdf.w - 2 * MARGIN
        usable_h = pdf.h - 2 * MARGIN
        scale = min(usable_w / width, usable_h / height)
        draw_w, draw_h = width * scale, height * scale
        pdf.image(
            buffer,
            x=MARGIN + (usable_w - draw_w) / 2,
            y=MARGIN + (usable_h - draw_h) / 2,
            w=draw_w,
            h=draw_h,
        )
        pages += 1

    if not pages:
        raise ConversionError("no images found")

    return bytes(pdf.output())


def _cell(value):
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 3] + "..."
    return text


def table_to_pdf(rows, title=None):
    """Render a list of rows (first row = header) as a PDF table."""
    cleaned = [[_cell(cell) for cell in row] for row in rows if row is not None]
    cleaned = [row for row in cleaned if any(cell for cell in row)]

    if not cleaned:
        raise ConversionError("the file looks empty")

    columns = max(len(row) for row in cleaned)
    cleaned = [row + [""] * (columns - len(row)) for row in cleaned]

    pdf, _has_font = _new_pdf(orientation="L" if columns > 6 else "P")

    pdf.add_page()

    if title:
        _body_font(pdf, "B", 13)
        pdf.multi_cell(0, 8, _cell(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    font_size = 9 if columns <= 8 else 7.5 if columns <= 12 else 6.5
    _body_font(pdf, size=font_size)

    try:
        with pdf.table(
            width=pdf.w - 2 * MARGIN,
            text_align="LEFT",
            line_height=font_size * 0.55,
            padding=1.4,
            first_row_as_headings=True,
        ) as table:
            for row in cleaned:
                table_row = table.row()
                for cell in row:
                    table_row.cell(cell)
    except Exception as exc:
        raise ConversionError("could not lay out the table") from exc

    return bytes(pdf.output())


def decode_text(raw):
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def text_to_pdf(text):
    """Render plain text (or a .md/.txt file) as a simple flowing document."""
    pdf, _has_font = _new_pdf()

    pdf.add_page()
    _body_font(pdf, size=11)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ConversionError("the file looks empty")

    for line in text.split("\n"):
        pdf.multi_cell(0, 6.2, line if line.strip() else " ", new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


def read_table(filename, stream):
    """Read .csv / .xlsx / .xlsm into a list of rows."""
    name = (filename or "").lower()

    if name.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ConversionError("spreadsheet support is unavailable") from exc

        try:
            workbook = load_workbook(stream, data_only=True, read_only=True)
            sheet = workbook[workbook.sheetnames[0]]
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            workbook.close()
            return rows
        except Exception as exc:
            raise ConversionError("could not read that spreadsheet") from exc

    if name.endswith(".csv"):
        try:
            text = decode_text(stream.read())
            dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|")
        except Exception:
            text = decode_text(stream.read())
            dialect = csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect)]

    raise ConversionError("use a .csv or .xlsx file")
