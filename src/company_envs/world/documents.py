"""Render seeded desktop files into the formats their names promise.

The seeding model writes every material as text under the file name a real desktop would have
(``Downloads/Invoice 4471.pdf``, ``Documents/Q3 forecast v2.xlsx``, ``deck.pptx``). Text is what
reviewers and readability checks read, so the company folder keeps the text. At VM launch this
module turns the text into the real file: PDF, Word, Excel and PowerPoint via pure-Python
writers; everything else passes through unchanged.

Conventions the skill states: a spreadsheet's text is CSV, one sheet per ``## Sheet: name``
block; a deck's text is one slide per ``---`` line, first line the slide title; a document's
text uses ``#`` headings and blank-line paragraphs.
"""

import csv
import hashlib
import io
import re
from pathlib import Path

# Material folders that land in the guest home as written; anything else goes on the Desktop.
# Videos was missing, so a material named Videos/clip.mp4 landed at Desktop/Videos/clip.mp4 --
# not where a worker or an application looks for it, and not where the Videos folder in the
# guest's own sidebar points. No world ships media yet, so nothing on disk moves; the folder has
# to be right before one does.
HOME_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")
# The folders a grader can see, read by both halves of the round trip: the guest export in
# backends/mypcbench.download_desktop and the file-predicate rule in grader_core.Predicate.
#
# It used to be Desktop, Documents and Downloads, hard-coded separately in each -- so a material
# seeded under Pictures/, Music/ or Videos/ was opened by an application and could never be
# graded: the baseline hashed it and the episode never returned it, and a predicate naming it was
# refused at authoring time. Widening one half alone would have reached nobody, which is why this
# is one list in one place: the next folder added to HOME_FOLDERS cannot be added to one side.
GRADED_FOLDERS = HOME_FOLDERS


def guest_path(name):
    """Where a world material lands relative to the guest home directory."""
    parts = Path(name).parts
    if parts and parts[0] in HOME_FOLDERS:
        return str(Path(*parts))
    return f"Desktop/{name}"


FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
RENDERED = {".pdf", ".docx", ".xlsx", ".pptx"}


def _text(content):
    return content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)


def _paragraphs(text):
    """Blocks of the source as (kind, level, text), losing nothing.

    A heading used to swallow its whole block: the pattern matched the first line and yielded only
    that, so an invoice whose block was "# Sandstone Printer Care / Invoice 4471 / Issued ... /
    Customer: ..." rendered as the company name alone. The number, both dates and the customer were
    dropped from the PDF the worker opens, 151 times across 130 of the 1,027 documents built at
    launch, and no gate could see it because every gate reads the source rather than the artifact.

    Lines are kept apart for the same reason: joining a label-and-value block with spaces turned
    line items into one run-on sentence in 127 files, and the sources do not hard-wrap, so there
    was never prose that needed rejoining.
    """
    for block in re.split(r"\n\s*\n", text.strip()):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        match = re.match(r"^(#{1,3})\s+(.*)$", lines[0])
        if match:
            yield "h", len(match.group(1)), match.group(2).strip()
            lines = lines[1:]
        # Every line is its own paragraph. Joining them was meant to rewrap hard-wrapped prose,
        # but the sources are not hard-wrapped: only 11% of blocks have more than one line and a
        # single line runs to 1,249 characters, so a multi-line block is almost always records --
        # a label and value, an invoice line item. Joining those produced 343 run-on blocks.
        for line in lines:
            line = line.strip()
            if line:
                yield "p", 0, line


def render_pdf(text):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    font = next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)
    if font:
        pdf.add_font("Body", "", font)
        family = "Body"
    else:
        family = "Helvetica"
        text = text.encode("latin-1", errors="replace").decode("latin-1")
    for kind, level, para in _paragraphs(text):
        pdf.set_font(family, size=16 - 2 * level if kind == "h" else 11)
        pdf.multi_cell(0, 6 if kind == "p" else 8, para)
        pdf.ln(2)
    return bytes(pdf.output())


def render_docx(text):
    from docx import Document

    doc = Document()
    for kind, level, para in _paragraphs(text):
        if kind == "h":
            doc.add_heading(para, level=min(level, 3))
        else:
            doc.add_paragraph(para)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _sheets(text):
    parts = re.split(r"(?m)^##\s*Sheet:\s*(.+)$", text.strip())
    if len(parts) == 1:
        return [("Sheet1", parts[0])]
    sheets = []
    if parts[0].strip():
        sheets.append(("Sheet1", parts[0]))
    for name, body in zip(parts[1::2], parts[2::2]):
        sheets.append((name.strip()[:31] or "Sheet", body))
    return sheets


# A workbook openpyxl writes stores no column widths, so Calc opens every column at the file
# format's 8.43-character default and clips whatever is wider: a live guest showed a seeded
# pipeline as "Northwir...", "Priya Rag...", "Negotiati...". Across the 266 workbooks on disk,
# 2,206 of 2,811 columns hold a value wider than the default and 6,621 of 14,171 values are
# clipped -- 78% of columns and 47% of cells. The cap is there because a 263-character note is a
# note, not a column that wide; past the cap Calc spills the text across the empty cells beside
# it, which is what a person sees in a real sheet, and clips only where a neighbour has content.
DEFAULT_COLUMN_WIDTH = 8.43
MAX_COLUMN_WIDTH = 60
COLUMN_PADDING = 2


def _fit_columns(sheet):
    """Set each column to its widest value, so a real company's values are readable on open."""
    from openpyxl.utils import get_column_letter

    widest = {}
    for row in sheet.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            longest = max((len(line) for line in str(cell.value).splitlines()), default=0)
            widest[cell.column] = max(widest.get(cell.column, 0), longest)
    for index, width in widest.items():
        sheet.column_dimensions[get_column_letter(index)].width = min(
            MAX_COLUMN_WIDTH, max(DEFAULT_COLUMN_WIDTH, width + COLUMN_PADDING)
        )


def render_xlsx(text):
    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for name, body in _sheets(text):
        sheet = book.create_sheet(title=re.sub(r"[\[\]:*?/\\]", "-", name))
        for row in csv.reader(io.StringIO(body.strip())):
            sheet.append([_cell(value) for value in row])
        _fit_columns(sheet)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _cell(value):
    value = value.strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def render_pptx(text):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    deck = Presentation()
    for slide_text in re.split(r"(?m)^---\s*$", text.strip()):
        lines = [line.rstrip() for line in slide_text.strip().splitlines() if line.strip()]
        if not lines:
            continue
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = lines[0].lstrip("# ").strip()
        if len(lines) > 1:
            box = slide.shapes.add_textbox(Inches(0.7), Inches(1.6), Inches(8.6), Inches(5))
            frame = box.text_frame
            frame.word_wrap = True
            for index, line in enumerate(lines[1:]):
                paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
                paragraph.text = line.lstrip("-• ").strip()
                paragraph.font.size = Pt(18)
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


# Pictures the guest can actually draw
# ------------------------------------
# A world's only image materials are 6 SVGs, and 5 of them are stubs whose single <image> points
# at https://assets.lakebridgeaudience.com/... . A worker VM runs QEMU restrict=on, so that fetch
# can never happen: GIMP and the Image Viewer open on an empty canvas, and the visual gate's
# "an image the browser cannot draw" rule is the web-side name for the same defect. Both are
# fixed here, where the file is written, rather than in each application.
IMAGE_SIZE = (1080, 720)
REMOTE_IMAGE = re.compile(r"<image\b[^>]*?href\s*=\s*[\"'](https?://[^\"']*)[\"'][^>]*/?>", re.IGNORECASE)


def _asset_colour(text):
    """A flat, readable ground, decided by the content so a relaunch draws the same picture.

    Same hue family as ``placeholder_images._colour``, which is what the browser side draws, so
    a company's assets look like one set whichever half of the runtime renders them.
    """
    import colorsys

    seed = hashlib.sha256(text.encode()).digest()[0]
    red, green, blue = colorsys.hls_to_rgb(seed / 256, 0.42, 0.44)
    return tuple(round(channel * 255) for channel in (red, green, blue))


def _asset_lines(text):
    """The asset's own words: its title first, then whatever the material says about it."""
    stripped = []
    for line in _plain(text).splitlines():
        line = line.strip().lstrip("# ").strip()
        if line and line not in stripped:
            stripped.append(line)
    return stripped[:8] or ["Company asset"]


def _plain(text):
    """Markup out, words in: an SVG material's text is XML, and its tags are not the asset."""
    return re.sub(r"<[^>]+>", "\n", text) if text.lstrip().startswith("<") else text


def render_raster(text):
    """A real picture carrying the company's own description of the asset.

    The alternative is what the worlds ship today -- a reference to a host the guest cannot
    reach, which is a blank canvas in GIMP and a broken box in a browser. Drawn rather than
    fetched, and drawn from the material's own text, so the picture is the company's.
    """
    from PIL import Image, ImageDraw, ImageFont

    ground = _asset_colour(text)
    image = Image.new("RGB", IMAGE_SIZE, (247, 246, 243))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, IMAGE_SIZE[0], 210), fill=ground)
    font_file = next((f for f in FONT_CANDIDATES if Path(f).is_file()), None)
    title_font = ImageFont.truetype(font_file, 46) if font_file else ImageFont.load_default()
    body_font = ImageFont.truetype(font_file, 26) if font_file else ImageFont.load_default()
    lines = _asset_lines(text)
    draw.text((56, 78), lines[0][:52], fill=(255, 255, 255), font=title_font)
    for index, line in enumerate(lines[1:]):
        draw.text((56, 268 + index * 46), line[:88], fill=(38, 40, 44), font=body_font)
    return image


def render_png(text):
    out = io.BytesIO()
    render_raster(text).save(out, format="PNG")
    return out.getvalue()


def render_jpeg(text):
    out = io.BytesIO()
    render_raster(text).save(out, format="JPEG", quality=88)
    return out.getvalue()


def render_svg(text):
    """The company's own vector, with every unreachable <image> replaced by something drawable.

    Only the remote references move. An SVG that draws itself -- 1 of the 6 on disk does -- comes
    back byte for byte, because the company's artwork is the company's.
    """
    if not REMOTE_IMAGE.search(text):
        return text.encode()
    title = (re.search(r"<title>(.*?)</title>", text, re.DOTALL) or [None, "Company asset"])[1].strip()
    colour = "#" + "".join(f"{channel:02x}" for channel in _asset_colour(text))
    width, height = IMAGE_SIZE

    def drawn(match):
        return (
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="{colour}"/>'
            f'<text x="{width // 2}" y="{height // 2}" fill="#ffffff" font-family="DejaVu Sans, sans-serif" '
            f'font-size="34" text-anchor="middle" dominant-baseline="central">{title[:60]}</text>'
        )

    return REMOTE_IMAGE.sub(drawn, text).encode()


RENDERERS = {
    ".pdf": render_pdf,
    ".docx": render_docx,
    ".xlsx": render_xlsx,
    ".pptx": render_pptx,
    ".png": render_png,
    ".jpg": render_jpeg,
    ".jpeg": render_jpeg,
    ".svg": render_svg,
}


# The first bytes of a file that has already been rendered. Rendering one again would treat its
# own bytes as text: a PNG re-rendered draws the words "PNG IHDR IDAT" on a canvas, which is what
# GIMP opened on a live guest before this list grew past the two document magics. The guard is
# not paranoia -- ``render_material`` is called on whatever the plan holds, and a plan built from
# an already-staged tree holds artifacts.
ALREADY_RENDERED = (b"%PDF", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8")


def render_material(path, content):
    """Bytes for the desktop: rendered when the name promises a document format, else as-is."""
    suffix = Path(path).suffix.lower()
    if suffix not in RENDERERS:
        return content
    if isinstance(content, bytes) and content.startswith(ALREADY_RENDERED):
        return content  # already a real file
    return RENDERERS[suffix](_text(content))
