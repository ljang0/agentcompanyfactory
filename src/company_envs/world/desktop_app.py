"""Open a seeded material in the desktop application its name promises, and read it back.

``documents.render_material`` already turns a seeded material's text into a real ``.pdf``,
``.docx``, ``.xlsx`` or ``.pptx``, and ``hub_vm`` already uploads the rendered tree into the
guest home. What that leaves is the other half of a runtime adapter: which application opens
which material, how to know it is open on the company's data rather than on a modal, and how to
read the file back afterwards so a grader can assert on what the worker actually changed.

Three facts are load-bearing and were measured on a live guest rather than assumed.

**The X display is ``:1`` on this image, not ``:0``.** Every CUA-Gym skill hard-codes
``DISPLAY=:0``; on our base image that opens nothing and says nothing. Nothing here names a
display: the preamble is ``backends.mypcbench.DESKTOP``, which finds the session's socket, and it
is imported rather than copied because two definitions of "which display" is exactly how the
:0 assumption comes back.

**A killed LibreOffice leaves Document Recovery state**, and the controller stops episode VMs
rather than shutting their applications down, so the next launch opens a modal over the work.
Every LibreOffice launch line therefore carries ``--norestore``, which is the only lever the
adapter holds: a clean shutdown here would cover the launcher's own kills and not the two that
matter, the end of the episode and the worker's own. Suppressing the first-run wizard and the
Tip of the Day belongs in the base image for the same reason -- they are profile state, and the
adapter never owns the profile.

**Reading is the part that did not exist.** ``documents.py`` only writes. The readers below are
the CUA-Gym reward-gen recipes ported to our layout (``/home/ga/<guest_path>`` rather than
``/home/user/<task_id>_initial.xlsx``): openpyxl for a workbook, python-docx for a document,
python-pptx for a deck, pypdf for a PDF. They need no dependency this repo does not already have.

Severity follows the house rule. A catalog that could never work -- a launch line with no
``{path}``, two applications claiming one suffix, a reader that is not implemented, a format
``documents.py`` writes that nothing opens -- is an authoring-time error, raised where the author
is still in the loop. An observation about a world already on disk -- a material no application
opens, a file that will not read back -- is a warning, because no stage can repair a material
whose name was chosen rounds ago.
"""

import csv
import io
import json
import re
import shlex
from functools import cache
from pathlib import Path

from .backends.mypcbench import DESKTOP
from .documents import RENDERERS, guest_path
from .world_check import finding

CATALOG_PATH = Path(__file__).parent.parent.parent.parent / "catalogs/desktop_apps.json"
GUEST_HOME = "/home/ga"
# Required on every catalog entry. `opens` may be empty (Files opens a folder, not a material).
REQUIRED_FIELDS = ("name", "packages", "launch", "opens", "reader", "ready_seconds", "settle_seconds")


@cache
def _catalog_text(path):
    return Path(path).read_text() if Path(path).is_file() else "{}"


def desktop_apps(path=None):
    """The desktop applications this adapter may launch, by id; empty when the catalog is absent.

    A fresh dict every call: the parse is cached, the result is not, so a caller that edits an
    entry cannot change what the next caller reads.
    """
    return json.loads(_catalog_text(str(path or CATALOG_PATH))).get("apps", {})


def app_ids(path=None):
    return set(desktop_apps(path))


# ---------------------------------------------------------------------------- readers


def read_sheets(path, *, data_only=True):
    """Every cell of every sheet, in row order: the openpyxl half of the Calc recipe.

    ``data_only`` picks which of a workbook's two answers to read. True is the cached value an
    application wrote when it last saved -- what the worker saw on screen -- and is None in a
    workbook no application has opened. False is what is stored: a literal, or the formula's own
    text. A check that reads only one of them is silently blind to workbooks of the other kind,
    which is why ``grader_core``'s ``sheet_contains`` asks for both.
    """
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=data_only)
    try:
        sheets = {
            name: [[cell for cell in row] for row in book[name].iter_rows(values_only=True)]
            for name in book.sheetnames
        }
    finally:
        book.close()
    # The sheet name is part of what the worker sees -- it is the tab at the bottom of the window,
    # and the seeding convention (``## Sheet: Requests``) makes it a company word, not a label.
    # Left out of the flat text, 10 of 12 sampled workbooks read back as having lost a word that
    # is plainly on screen.
    text = "\n".join(
        "\n".join([name, *("\t".join("" if cell is None else str(cell) for cell in row) for row in rows)])
        for name, rows in sheets.items()
    )
    return {"kind": "sheets", "sheets": sheets, "text": text}


def read_delimited(path):
    """A CSV read as the grid Calc shows, so the same assertions work on it as on a workbook."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    rows = [row for row in csv.reader(io.StringIO(raw))]
    return {"kind": "sheets", "sheets": {"Sheet1": rows}, "text": raw}


def read_document(path):
    """Paragraphs and table cells. Tables are read because a Writer table is where a worker
    puts a value a grader looks for, and ``paragraphs`` alone does not contain them."""
    from docx import Document

    document = Document(str(path))
    paragraphs = [p.text for p in document.paragraphs]
    tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in document.tables]
    text = "\n".join(paragraphs + [cell for table in tables for row in table for cell in row])
    return {"kind": "document", "paragraphs": paragraphs, "tables": tables, "text": text}


def read_slides(path):
    """One entry per slide: title, the lines of every text frame, table cells and speaker notes."""
    from pptx import Presentation

    deck = Presentation(str(path))
    slides = []
    for slide in deck.slides:
        lines, tables = [], []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            if getattr(shape, "has_table", False):
                tables.append([[cell.text for cell in row.cells] for row in shape.table.rows])
        notes = ""
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
        title = slide.shapes.title.text if slide.shapes.title is not None else ""
        slides.append({"title": title, "lines": lines, "tables": tables, "notes": notes})
    text = "\n".join(
        [line for slide in slides for line in slide["lines"]]
        + [cell for slide in slides for table in slide["tables"] for row in table for cell in row]
        + [slide["notes"] for slide in slides if slide["notes"]]
    )
    return {"kind": "slides", "slides": slides, "text": text}


def read_pdf(path):
    """Page text, the way ``grader_core`` already reads a PDF, kept per page so a check can say
    which page a value is on."""
    from pypdf import PdfReader

    with Path(path).open("rb") as stream:
        pages = [page.extract_text() or "" for page in PdfReader(stream).pages]
    return {"kind": "pdf", "pages": pages, "text": "\n".join(pages)}


def read_text(path):
    return {"kind": "text", "text": Path(path).read_text(encoding="utf-8", errors="replace")}


SVG_SIZE = re.compile(r'\b(width|height)\s*=\s*"([0-9.]+)', re.IGNORECASE)


def read_image(path):
    """Size and format. An SVG is XML, so it is read as markup and its declared size taken from
    the root attributes; Pillow cannot open one and would report a picture material as unreadable."""
    path = Path(path)
    if path.suffix.lower() == ".svg":
        markup = path.read_text(encoding="utf-8", errors="replace")
        size = {key.lower(): float(value) for key, value in SVG_SIZE.findall(markup[:2000])}
        return {
            "kind": "image",
            "format": "SVG",
            "width": size.get("width"),
            "height": size.get("height"),
            "text": markup,
        }
    from PIL import Image

    with Image.open(path) as image:
        return {
            "kind": "image",
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "text": "",
        }


# What a reader can prove about a file, which is what decides whether an application yields
# gradeable tasks. "content" is a value a check can name -- a cell, a paragraph, a slide's text.
# "properties" is size and format and nothing about what is in the picture. "none" is an
# application that opens a company's file and leaves nothing behind that any check can read.
GRADING = {
    "sheets": "content",
    "delimited": "content",
    "document": "content",
    "slides": "content",
    "pdf": "content",
    "text": "content",
    "image": "properties",
    "none": "none",
}

READERS = {
    "sheets": read_sheets,
    "delimited": read_delimited,
    "document": read_document,
    "slides": read_slides,
    "pdf": read_pdf,
    "text": read_text,
    "image": read_image,
    "none": None,
}
# What each reader can actually parse. An application opens more than this -- Calc opens ``.ods``
# and Writer opens ``.doc`` -- and a worker who saves into one of those formats is an observation
# about that episode, not a broken catalog, so this table is narrower than any app's ``opens``.
READER_SUFFIXES = {
    "sheets": (".xlsx", ".xlsm"),
    "delimited": (".csv", ".tsv"),
    "document": (".docx",),
    "slides": (".pptx",),
    "pdf": (".pdf",),
    "text": (
        ".txt",
        ".md",
        ".log",
        ".json",
        ".py",
        ".c",
        ".h",
        ".cpp",
        ".go",
        ".rs",
        ".sh",
        ".sql",
        ".yaml",
        ".yml",
        ".toml",
        ".mjs",
        ".js",
        ".ts",
        ".tsx",
        ".html",
        ".eml",
        ".csv",
        # An OpenShot project is JSON, which is why that application is gradeable on content at
        # all: a check can name the clip, its position or the project's title.
        ".osp",
    ),
    "image": (".svg", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"),
    "none": (),
}


def grading_of(app, name=None):
    """Whether this application's files can be checked on content, on properties, or not at all.

    Per file when a name is given, because an application's formats do not all answer the same
    question: GIMP opens a .png, whose reader can only report size and format, and an .svg, which
    is markup and so carries values a check can name. Answering per application would have called
    the 6 real image materials ungradeable when every one of them is text.
    """
    reader = app.get("reader")
    if name is not None:
        suffix = Path(name).suffix.lower()
        reader = (app.get("reader_by_suffix") or {}).get(suffix, reader)
        if reader == "image" and suffix == ".svg":
            return "content"
    return GRADING.get(reader, "none")


def app_for_material(name, apps=None):
    """``(app_id, app)`` for the application that opens this material's format, else ``(None, None)``."""
    suffix = Path(name).suffix.lower()
    for app_id, app in sorted((apps if apps is not None else desktop_apps()).items()):
        if suffix in app.get("opens", ()):
            return app_id, app
    return None, None


def reader_for(name, apps=None):
    """The reader that grades this material, or None when no application opens it."""
    suffix = Path(name).suffix.lower()
    _, app = app_for_material(name, apps)
    if app is None:
        return None
    return (app.get("reader_by_suffix") or {}).get(suffix, app["reader"])


def read_material(path, apps=None):
    """The material as a grader reads it: ``{"kind", "text", ...}``.

    Raises for a format the contract does not read. That is deliberate -- a grader asking for a
    value out of a format nothing can parse is an authoring mistake, and a silent empty read is
    the "empty run writes the receipt a real verdict writes" failure in miniature.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    reader = reader_for(path.name, apps)
    if reader is None:
        raise ValueError(f"no desktop application opens {suffix or path.name!r}")
    if reader == "none" or suffix not in READER_SUFFIXES[reader]:
        raise ValueError(f"the {reader!r} reader does not parse {suffix!r}: {path.name}")
    return READERS[reader](path)


def material_text(path, apps=None):
    """Flat text of a material, for a ``contains`` assertion that does not care about structure."""
    return read_material(path, apps)["text"]


# ---------------------------------------------------------------------------- launching


def launch_command(app, path, *, home=GUEST_HOME):
    """A shell line that opens ``path`` in ``app`` inside the guest's own session and returns.

    ``setsid`` with output redirected and stdin closed is what lets the SSH command finish: the
    application keeps running after the transport's process group is killed, which is how every
    action in ``backends.mypcbench`` ends.
    """
    target = guest_target(path, home=home)
    suffix = Path(target).suffix.lower()
    template = (app.get("launch_by_suffix") or {}).get(suffix, app["launch"])
    if "{path}" not in template and "{home}" not in template:
        raise ValueError(f"launch template has no {{path}} or {{home}} placeholder: {template!r}")
    # {home} as well as {path}: VS Code is opened on the worker's folder and then the file, which
    # is what puts an explorer beside the editor rather than a lone tab, and Files opens a folder
    # and no file at all.
    line = template.replace("{home}", shlex.quote(home.rstrip("/"))).replace("{path}", shlex.quote(target))
    return DESKTOP + f"setsid {line} >/dev/null 2>&1 </dev/null &\n"


def guest_target(path, *, home=GUEST_HOME):
    """Where a material lives in the guest: an absolute path is taken as given, a material name
    goes through ``documents.guest_path`` so the launcher and the grader name the same file."""
    text = str(path)
    return text if text.startswith("/") else f"{home.rstrip('/')}/{guest_path(text)}"


# xdotool is how every other guest interaction in this repo reads the screen, so readiness,
# the window list and the covering-modal check all go through it rather than through a second
# mechanism that could disagree with the click coordinates.
GUEST_WINDOWS = r"""
import json, subprocess, sys, time

def xdotool(*args, empty_ok=False):
    for attempt in (0, 1):
        try:
            done = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=20)
        except subprocess.SubprocessError:
            continue  # a timeout under host load is not a missing tool; try once more
        except OSError:
            return None
        if done.returncode == 0:
            return done.stdout
        # xdotool search exits 1 when nothing matched, which is an answer, not a failure.
        if empty_ok and not done.stderr.strip():
            return ""
        return None
    return None

def windows():
    listed = xdotool("search", "--onlyvisible", "--name", ".", empty_ok=True)
    if listed is None:
        return None
    found = []
    for handle in listed.split():
        name, shell = xdotool("getwindowname", handle), xdotool("getwindowgeometry", "--shell", handle)
        if name is None or shell is None:
            continue
        box = {}
        for line in shell.splitlines():
            key, _, value = line.partition("=")
            if value.strip().lstrip("-").isdigit():
                box[key.strip().lower()] = int(value)
        found.append({"id": handle, "name": name.strip(), **box})
    return found

titles, seconds = json.loads(sys.argv[1])
started = time.monotonic()
while True:
    found = windows()
    if found is None:
        print(json.dumps({"xdotool": False, "windows": [], "waited": 0.0}))
        break
    waited = round(time.monotonic() - started, 1)
    ready = any(title in window["name"] for title in titles for window in found)
    if ready or waited >= seconds:
        print(json.dumps({"xdotool": True, "windows": found, "waited": waited}))
        break
    time.sleep(1)
"""


def windows_command(titles=(), seconds=0):
    """A shell line that prints the guest's visible windows as JSON, once one of ``titles`` shows.

    An empty screen and a missing xdotool are different answers. ``xdotool search`` exits 1 when
    nothing matched, and reading that as "the tool is not there" reported a working VS Code as
    having no window manager at all; a timeout under host load is retried once for the same
    reason -- a slow guest is not an absent one.

    ``seconds`` of 0 lists what is on screen right now; a positive value polls until a window
    carries one of the fragments. Waiting on the window rather than sleeping a fixed time is the
    difference between "the app is open" and "twenty-five seconds have passed", and a cold
    LibreOffice on this image is the reason the two are not the same number.
    """
    payload = json.dumps([_titles(titles), float(seconds)])
    return DESKTOP + f"python3 -c {shlex.quote(GUEST_WINDOWS)} {shlex.quote(payload)}"


def _titles(titles):
    return [titles] if isinstance(titles, str) else [title for title in titles if title]


def window_titles(app, name):
    """What proves this application is showing this document, most telling first.

    Measured on a live guest, because no rule covers all six: evince titles its window with the
    file name alone, LibreOffice with "<name> - LibreOffice Calc", gedit with
    "<name> (~/Downloads) - gedit" -- and the GNOME Image Viewer with neither, just "Image
    Viewer", because it puts the file name in a client-side header bar the window manager never
    sees. Waiting only for the file name left eog polling for its full 60 seconds and then
    reporting a working viewer as not open, so the application's own name is a fallback and the
    report says which of the two matched.
    """
    wanted = [Path(name).name, Path(name).stem, app.get("window_match") or ""]
    return tuple(dict.fromkeys(title for title in wanted if title))


# Window titles the session itself owns; they are not any application's message about a document.
# Measured, not guessed: on this image every screen carries "mutter guard window" (the compositor's
# fullscreen guard) and "@!0,0;BDHF" (gnome-shell's own window), both full-screen, so a covering
# rule that did not know them reported a modal over every one of the six applications.
DESKTOP_CHROME = re.compile(r"^(?:gnome-shell|mutter guard window|@!.*|Desktop|Activities|Top Bar)$")
# What an application says in its title bar when it did not open the work. "Document Recovery" is
# the one that a stopped episode VM causes; the rest are the first-run and failure dialogs.
ERROR_WINDOWS = re.compile(
    r"document recovery|tip of the day|recovery|read.?error|could not|cannot open|"
    r"error|failed|crash|repair|password|authentication|unsupported|read-only",
    re.IGNORECASE,
)


def window_error(name, titles=()):
    """The failure a window's title announces, or None -- with the document's own name removed.

    ``interact_check``'s NaN bug in desktop form. A window title is the document's name plus the
    application's message, so matching a failure phrase against the whole title reports Document
    Recovery over a working screen whenever the company named the file that way -- 15 of the
    2,026 materials on disk do, among them "Invoice 6814 - Brook Recovery.pdf", "Repair source
    packet.pdf" and "recovery_attempts_0905.log". The name is the world's, not the application's,
    so it is removed before the phrases are looked for.
    """
    remainder = name
    for title in _titles(titles):
        remainder = remainder.replace(title, " ")
    found = ERROR_WINDOWS.search(remainder)
    return remainder[max(0, found.start() - 30) : found.end() + 30].strip() if found else None


def _overlap(one, other):
    for box in (one, other):
        if not {"x", "y", "width", "height"} <= box.keys():
            return 0
    wide = min(one["x"] + one["width"], other["x"] + other["width"]) - max(one["x"], other["x"])
    tall = min(one["y"] + one["height"], other["y"] + other["height"]) - max(one["y"], other["y"])
    return max(0, wide) * max(0, tall)


def document_window(windows, titles):
    """The window showing this document, or None.

    The fragments are tried in order, so the file name beats the application's name and the
    report can say which one answered. Within a fragment the largest match wins: LibreOffice's
    splash and its progress window carry the document name too, and the work is the big one.
    """
    for title in _titles(titles):
        named = [window for window in windows if title in window.get("name", "")]
        if named:
            return max(named, key=lambda window: window.get("width", 0) * window.get("height", 0))
    return None


def covering_windows(windows, titles, *, min_fraction=0.01):
    """Windows sitting over the document: the desktop's answer to "is anything covering the data".

    A modal is what a screenshot of a working application looks like when it is not working --
    the first Calc probe on this image showed the seeded rows behind a Tip of the Day dialog and
    a release-notes banner. Session chrome is excluded by name, and a sliver of overlap is
    ignored, because a panel touching a maximised window is not a modal over the work.
    """
    document = document_window(windows, titles)
    if document is None:
        return []
    area = max(1, document.get("width", 0) * document.get("height", 0))
    return [
        window
        for window in windows
        if window.get("id") != document.get("id")
        and not DESKTOP_CHROME.match(window.get("name", ""))
        and _overlap(window, document) > area * min_fraction
    ]


def close_command(window_id):
    """Ask the window manager to close a window, rather than killing the process behind it.

    ``windowclose`` sends WM_DELETE_WINDOW; ``windowkill`` destroys the client, and a destroyed
    LibreOffice is what writes the Document Recovery state the next launch opens a modal over.
    This is the only teardown the adapter owns -- the end of an episode is the controller
    stopping the VM, and a worker can close an application any way they like -- which is why
    ``--norestore`` on the launch line, not this, is what makes the next launch clean.

    By window id, not by name: closing by a name search left the Image Viewer open behind the
    next four applications, because its window is not called after the file it is showing.
    """
    return DESKTOP + f"xdotool windowclose {shlex.quote(str(window_id))}"


def kill_command(app):
    """End this application the way an episode does: uncleanly, from outside.

    The pattern's first character is bracketed -- ``[b]lender`` instead of ``blender`` -- because
    ``pkill -f`` matches the whole command line and the command line running pkill contains the
    pattern. Unbracketed, the first live run killed its own shell before it killed Blender: no
    output came back, the application was still on screen, and the relaunch check passed against
    an application that had never stopped. That is an empty run writing a real verdict's receipt,
    and it is the fourth time tonight a self-matching pattern has bitten this session.
    """
    pattern = app["kill"]
    bracketed = f"[{pattern[0]}]{pattern[1:]}" if pattern else pattern
    return f"pkill -9 -f -- {shlex.quote(bracketed)}; echo killed=$?"


def maximise_command(window_id):
    """Fill the screen with the document, the way a person would have.

    An application at its default size is not what a worker's desktop looks like: on this image
    evince opened 600x600 and gedit 952x799 on a 1280x800 screen, and the rest was wallpaper --
    the presentation defect the web half has a visual judge for.

    ``wmctrl`` and not ``xdotool``, measured: this image's xdotool has no ``windowstate`` command
    at all, and sizing by pixels is wrong here because ``_NET_WORKAREA`` reports the whole screen
    while the dock and the top bar in fact take 70 and 27 pixels of it. Asking the window manager
    to maximise put gedit at exactly 1210x773+70+27. Tolerated rather than required: an image
    without wmctrl leaves the window at its own size, which the report then shows.
    """
    return DESKTOP + (
        f"wmctrl -i -r {shlex.quote(str(window_id))} -b add,maximized_vert,maximized_horz || true"
    )


def type_commands(app, text):
    """Type ``text`` into the focused window and put it on disk, as a worker would.

    The desktop half of ``interact_check.mutation_probe``. Returns an empty list for an
    application a worker does not type into -- a viewer, a file manager, a media player -- rather
    than pretending: a probe that types into VLC and reports success would be measuring nothing.

    The keystrokes come from the catalogue because they differ per application and the difference
    is not guessable: LibreOffice answers every non-ODF save with a "Keep current format" dialog,
    so its save is ctrl+s *and* the Return that answers it, and a check that sent only ctrl+s
    would screenshot the modal and call it saved.
    """
    typing = app.get("typing")
    if not typing:
        return []
    quoted = shlex.quote(text)
    # A caret first, where one is not already waiting. Impress opens with no text box in edit
    # mode, so a generic "type into the focused window" reached nothing and the save still
    # rewrote the file -- a check failing against a deck that had been saved and not edited.
    focus = [
        f"xdotool key --clearmodifiers -- {shlex.quote(key)}; sleep 1" for key in typing.get("focus", [])
    ]
    keys = [f"xdotool key --clearmodifiers -- {shlex.quote(key)}" for key in typing.get("commit", [])]
    keys += [f"sleep 1; xdotool key --clearmodifiers -- {shlex.quote(key)}" for key in typing["save"]]
    return [
        DESKTOP + ("; ".join(focus) if focus else "true"),
        DESKTOP + f"xdotool type --clearmodifiers --delay 40 -- {quoted}",
        DESKTOP + "; ".join(keys) if keys else DESKTOP + "true",
    ]


def material_values(text, limit=40):
    """Values from a material a person would see on screen, for "is the company's data showing".

    ``interact_check.readable`` decides what counts, imported rather than restated: it is the rule
    that stopped a Chinese-language app reading as showing none of its records. Cells are taken
    before whole lines so a spreadsheet contributes its values and not its rows.
    """
    from .interact_check import readable

    found, seen = [], set()
    for line in str(text).splitlines():
        for value in [*(cell.strip() for cell in line.split(",")), line.strip()]:
            if readable(value) and value not in seen:
                seen.add(value)
                found.append(value)
            if len(found) >= limit:
                return found
    return found


def _matched_on(document, titles, title):
    """Whether the document's own name proved this window, or only the application's name.

    Worth reporting rather than hiding: "a window called Image Viewer exists" is weaker evidence
    than "a window called social-frame.svg exists", and a check that treated them as the same
    would call an application open on the wrong file open on the right one.
    """
    if document is None:
        return None
    matched = next((f for f in _titles(titles) if f in document.get("name", "")), None)
    if matched is None:
        return None
    return "filename" if matched in (title, Path(title).stem) else "application"


def open_material(run, name, *, app_id=None, apps=None, home=GUEST_HOME, seconds=None, maximise=True):
    """Launch the application that owns this material and report whether it is genuinely open.

    ``run`` is the caller's guest transport: any callable taking a shell string and returning its
    stdout. Returns the app, the command, what the guest's windows showed, the window holding the
    document, anything covering it and anything the titles announced.

    ``app_id`` names the application explicitly instead of routing by suffix. Routing is what an
    episode does -- a worker opens a file and its format decides -- but a check has to be able to
    test the application it says it is testing: without this, asking for GIMP on a .png silently
    proved the Image Viewer, because the Image Viewer is what that suffix routes to.
    """
    apps = desktop_apps() if apps is None else apps
    if app_id is not None:
        if app_id not in apps:
            raise ValueError(f"not a desktop application: {app_id}")
        app = apps[app_id]
    else:
        app_id, app = app_for_material(name, apps)
    if app is None:
        raise ValueError(f"no desktop application opens {Path(name).suffix.lower()!r}: {name}")
    target = guest_target(name, home=home)
    title = Path(target).name
    titles = window_titles(app, target)
    command = launch_command(app, target, home=home)
    run(command)
    wait = app["ready_seconds"] if seconds is None else seconds
    seen = json.loads(run(windows_command(titles, wait)) or "{}")
    windows = seen.get("windows", [])
    document = document_window(windows, titles)
    if document is not None and maximise:
        # After the window exists, not before: a window that is not mapped cannot be maximised,
        # and the state is re-read so the covering rule judges the geometry a worker will see.
        run(maximise_command(document["id"]))
        after = json.loads(run(windows_command()) or "{}")
        windows = after.get("windows", windows)
        document = document_window(windows, titles) or document
    return {
        "app_id": app_id,
        "path": target,
        "title": title,
        "titles": list(titles),
        "matched_on": _matched_on(document, titles, title),
        "command": command,
        "launch_seconds": seen.get("waited"),
        "settle_seconds": app["settle_seconds"],
        "xdotool": seen.get("xdotool", False),
        "windows": windows,
        "document_window": document,
        "covering": covering_windows(windows, titles),
        "window_errors": [
            error for window in windows if (error := window_error(window.get("name", ""), titles))
        ],
    }


# ---------------------------------------------------------------------------- checks


def check_desktop_catalog(apps=None, *, source="desktop_catalog"):
    """Errors for a catalog that could never launch or grade anything. Authoring-time only."""
    apps = desktop_apps() if apps is None else apps
    findings = []
    if not apps:
        return [finding("error", source, "apps", "no desktop applications are declared")]
    claimed = {}
    for app_id, app in sorted(apps.items()):
        where = f"apps/{app_id}"
        if missing := [field for field in REQUIRED_FIELDS if field not in app]:
            findings.append(finding("error", source, where, f"missing {', '.join(missing)}"))
            continue
        # An application that opens a folder rather than a file has no path to put in its line;
        # it still needs a placeholder, or the launcher would open whatever the shell's cwd is.
        wanted = "{path}" if app["opens"] else "{home}"
        if wanted not in app["launch"]:
            findings.append(finding("error", source, f"{where}/launch", f"no {wanted} placeholder"))
        for suffix, template in (app.get("launch_by_suffix") or {}).items():
            if suffix not in app["opens"]:
                findings.append(
                    finding("error", source, f"{where}/launch_by_suffix", f"{suffix} is not in opens")
                )
            if "{path}" not in template:
                findings.append(
                    finding("error", source, f"{where}/launch_by_suffix/{suffix}", "no {path} placeholder")
                )
        for reader in (app["reader"], *(app.get("reader_by_suffix") or {}).values()):
            if reader not in READERS:
                findings.append(finding("error", source, f"{where}/reader", f"{reader!r} is not implemented"))
        for suffix in app.get("reader_by_suffix") or {}:
            if suffix not in app["opens"]:
                findings.append(
                    finding("error", source, f"{where}/reader_by_suffix", f"{suffix} is not in opens")
                )
        if not app["packages"]:
            findings.append(finding("error", source, f"{where}/packages", "no package provides this app"))
        for seconds in ("ready_seconds", "settle_seconds"):
            if not isinstance(app[seconds], (int, float)) or app[seconds] < 0:
                findings.append(finding("error", source, f"{where}/{seconds}", "must be a number of seconds"))
        for suffix in app["opens"]:
            if suffix != suffix.lower() or not suffix.startswith("."):
                findings.append(finding("error", source, f"{where}/opens", f"{suffix!r} is not a suffix"))
            if suffix in claimed:
                findings.append(
                    finding(
                        "error", source, f"{where}/opens", f"{suffix} is also opened by {claimed[suffix]}"
                    )
                )
            claimed[suffix] = app_id
    # Every format documents.py writes must open somewhere and read back, or the pipeline is
    # rendering a file the runtime cannot use and the grader cannot see.
    for suffix in sorted(RENDERERS):
        app_id, app = app_for_material(f"x{suffix}", apps)
        if app is None:
            findings.append(
                finding(
                    "error", source, "opens", f"documents.py renders {suffix} and no application opens it"
                )
            )
            continue
        reader = reader_for(f"x{suffix}", apps)
        if suffix not in READER_SUFFIXES.get(reader, ()):
            findings.append(
                finding(
                    "error",
                    source,
                    f"apps/{app_id}/reader",
                    f"{suffix} is rendered by documents.py and the {reader!r} reader does not parse it",
                )
            )
    return findings


def check_materials(paths, apps=None, *, source="desktop_materials"):
    """Warnings about a world already on disk: a material nothing opens, or nothing can read.

    ``paths`` are rendered artifacts -- the guest tree ``hub_vm`` uploads, or files exported back
    out of a guest -- not the company's source text. A ``.xlsx`` under ``world/materials`` is CSV
    with ``## Sheet:`` blocks until ``documents.render_material`` has turned it into a workbook.

    Never an error. These names were chosen by the seeding model rounds ago and no stage rewrites
    a material's filename, so failing a world on one would be a stall with no repair path.
    """
    apps = desktop_apps() if apps is None else apps
    findings = []
    for path in paths:
        path = Path(path)
        _, app = app_for_material(path.name, apps)
        if app is None:
            findings.append(
                finding("warning", source, str(path), f"no desktop application opens {path.suffix.lower()!r}")
            )
            continue
        reader = reader_for(path.name, apps)
        if path.suffix.lower() not in READER_SUFFIXES.get(reader, ()):
            findings.append(
                finding(
                    "warning", source, str(path), f"opens in {app['name']} and the {reader!r} reader skips it"
                )
            )
            continue
        if not path.is_file():
            continue
        try:
            read_material(path, apps)
        except Exception as exc:  # noqa: BLE001 -- an unreadable material is the observation
            findings.append(
                finding("warning", source, str(path), f"{app['name']} reader failed: {type(exc).__name__}")
            )
    return findings


def validate_desktop_apps(ids, apps=None):
    """The desktop analogue of the hub adapter's "these apps are not seedable"."""
    apps = desktop_apps() if apps is None else apps
    if unknown := sorted(set(ids) - set(apps)):
        raise ValueError(
            "desktop runtime_adapter requires applications declared in catalogs/desktop_apps.json; "
            f"not launchable: {unknown}"
        )


__all__ = [
    "DESKTOP_CHROME",
    "ERROR_WINDOWS",
    "GRADING",
    "GUEST_HOME",
    "READERS",
    "READER_SUFFIXES",
    "app_for_material",
    "app_ids",
    "check_desktop_catalog",
    "check_materials",
    "close_command",
    "covering_windows",
    "desktop_apps",
    "document_window",
    "grading_of",
    "guest_target",
    "launch_command",
    "material_text",
    "material_values",
    "maximise_command",
    "open_material",
    "read_material",
    "reader_for",
    "type_commands",
    "validate_desktop_apps",
    "window_error",
    "window_titles",
    "windows_command",
]
