"""The desktop runtime adapter's contract: what opens a material, and what reads it back.

Every test here names a defect that was live on a guest booted today, or a trap the web half of
this sweep already paid for. The house style is that a test says why it exists.
"""

import copy
import json
import tomllib
from pathlib import Path

import pytest

from company_envs import pipeline
from company_envs.world.capabilities import ADAPTERS, runtime_mounting, validate_runtime_mounting
from company_envs.world.desktop_app import (
    READER_SUFFIXES,
    app_for_material,
    check_desktop_catalog,
    check_materials,
    close_command,
    covering_windows,
    desktop_apps,
    document_window,
    grading_of,
    guest_target,
    kill_command,
    launch_command,
    material_values,
    open_material,
    read_material,
    reader_for,
    type_commands,
    validate_desktop_apps,
    window_error,
    window_titles,
)
from company_envs.world.documents import (
    DEFAULT_COLUMN_WIDTH,
    GRADED_FOLDERS,
    HOME_FOLDERS,
    MAX_COLUMN_WIDTH,
    RENDERERS,
    guest_path,
    render_material,
)
from company_envs.world.grader_author import VALUE_OPERATORS
from company_envs.world.grader_core import (
    FILE_OPERATORS,
    Predicate,
    evaluate_predicate,
)

WORKBOOK = (
    "## Sheet: Q3 Pipeline\n"
    "Account,Owner,Stage,ARR,Close date\n"
    "Northwind Retail Group,Priya Raghavan,Negotiation,184000,2026-09-30\n"
    "Cedar Ridge Logistics,Marcus Okonkwo,Proposal,42500,2026-10-14\n"
    "## Sheet: Owners\n"
    "Owner,Territory\n"
    "Priya Raghavan,Northeast\n"
)


def test_no_launch_line_names_a_display():
    """Every CUA-Gym SKILL.md says DISPLAY=:0. This image's session is :1, and a launch against
    :0 opens nothing and prints nothing, so the mistake is invisible until a screenshot is blank."""
    for app in desktop_apps().values():
        command = launch_command(app, "Desktop/x" + (app["opens"][0] if app["opens"] else ".txt"))
        assert "DISPLAY=:0" not in command
        assert 'export DISPLAY=":${socket##*/X}"' in command  # the session's own socket, whichever it is


def test_every_libreoffice_launch_refuses_the_recovery_modal():
    """The controller stops episode VMs rather than closing their windows, and a LibreOffice that
    dies uncleanly leaves Document Recovery state: the next launch opens a modal over the work
    instead of the work. --norestore is the only lever the adapter holds over that."""
    office = [app for app in desktop_apps().values() if "libreoffice" in app["launch"]]
    assert office, "the catalog should still describe LibreOffice"
    for app in office:
        for template in (app["launch"], *(app.get("launch_by_suffix") or {}).values()):
            assert "--norestore" in template
    # And the adapter's own teardown asks the window manager, rather than destroying the client.
    assert "windowclose" in close_command("4194320")
    assert "windowkill" not in close_command("4194320")


def test_a_csv_opens_as_a_grid_and_not_as_an_import_wizard():
    """183 of the 2,026 materials on disk are .csv. Calc answers a plain `libreoffice --calc x.csv`
    with the Text Import dialog, which is a modal over the company's data, so the launch line
    pins the answer (comma, double quote, UTF-8, from row 1)."""
    calc = desktop_apps()["libreoffice_calc"]
    assert ".csv" in calc["opens"]
    assert "--infilter=" in calc["launch_by_suffix"][".csv"]
    assert "--infilter=" not in calc["launch"]  # a workbook needs no filter and must not get one


def test_every_format_documents_py_renders_opens_somewhere_and_reads_back():
    """The pipeline already builds .pdf/.docx/.xlsx/.pptx at VM launch. A format it renders that
    no application opens is a file the worker cannot use, and one no reader parses is a file the
    grader cannot see -- both are authoring errors, because both are ours to fix."""
    assert check_desktop_catalog() == []
    for suffix in RENDERERS:
        app_id, app = app_for_material(f"anything{suffix}")
        assert app is not None, suffix
        assert suffix in READER_SUFFIXES[reader_for(f"anything{suffix}")], (suffix, app_id)


def test_a_material_whose_own_name_says_recovery_is_not_reported_as_a_recovery_modal():
    """interact_check's NaN-in-Finance bug, in desktop form: a window title is the document's name
    plus the application's message, and 15 of the 2,026 materials on disk carry a failure word in
    their name -- "Invoice 6814 - Brook Recovery.pdf", "Repair source packet.pdf". Matched against
    the whole title, the window that proves the app opened reads as Document Recovery."""
    title = "Invoice 6814 - Brook Recovery.pdf"
    assert window_error(f"{title} - LibreOffice Writer", title) is None
    assert window_error("recovery_attempts_0905.log - gedit", "recovery_attempts_0905.log") is None
    # The application's own message still lands, on the same window, beside the same name.
    assert window_error(f"{title} - LibreOffice 7.3 Document Recovery", title) is not None
    assert "Tip of the Day" in window_error("Tip of the Day", title)


def test_a_dialog_over_the_document_is_covering_and_the_desktop_panel_is_not():
    """The first Calc probe on this image screenshotted the seeded rows behind a Tip of the Day
    dialog and a release-notes banner. A screenshot of that is not evidence the app works."""
    calc = {"id": "1", "name": "Q3 pipeline review.xlsx - LibreOffice Calc", "x": 70, "y": 27}
    calc |= {"width": 1210, "height": 773}
    tip = {"id": "2", "name": "Tip of the Day", "x": 384, "y": 328, "width": 576, "height": 250}
    panel = {"id": "3", "name": "gnome-shell", "x": 0, "y": 0, "width": 1280, "height": 27}
    files = {"id": "4", "name": "Documents", "x": 0, "y": 0, "width": 60, "height": 800}
    windows = [calc, tip, panel, files]
    title = "Q3 pipeline review.xlsx"
    assert document_window(windows, title) == calc
    assert [w["name"] for w in covering_windows(windows, title)] == ["Tip of the Day"]
    # Nothing open on the document at all is not "nothing is covering it".
    assert covering_windows([tip, panel], title) == []
    assert document_window([tip, panel], title) is None


def test_the_largest_window_carrying_the_name_is_the_document():
    """LibreOffice's splash and progress windows carry the document's name too, and answering
    "is it open" with the splash would call a five-second-old start-up a ready application."""
    splash = {"id": "1", "name": "Q3 pipeline review.xlsx", "x": 500, "y": 380, "width": 280, "height": 40}
    work = {"id": "2", "name": "Q3 pipeline review.xlsx - LibreOffice Calc"}
    work |= {"x": 70, "y": 27, "width": 1210, "height": 773}
    assert document_window([splash, work], "Q3 pipeline review.xlsx") == work


def test_open_material_waits_for_the_window_instead_of_sleeping_a_guess():
    """A fixed sleep answers "twenty-five seconds have passed", not "the app is open". A cold
    LibreOffice on this image is the reason those are not the same number."""
    issued = []

    def run(command):
        issued.append(command)
        if "getwindowname" not in command:
            return ""
        return (
            '{"xdotool": true, "waited": 11.0, "windows": [{"id": "1", '
            '"name": "Q3 pipeline review.xlsx - LibreOffice Calc", '
            '"x": 70, "y": 27, "width": 1210, "height": 773}]}'
        )

    opened = open_material(run, "Documents/Q3 pipeline review.xlsx", maximise=False)
    assert opened["app_id"] == "libreoffice_calc"
    assert opened["path"] == "/home/ga/Documents/Q3 pipeline review.xlsx"
    assert opened["launch_seconds"] == 11.0
    assert opened["document_window"]["id"] == "1"
    assert opened["matched_on"] == "filename"
    assert opened["covering"] == [] and opened["window_errors"] == []
    # The wait is bounded by the app's own ready_seconds, and carries the document's name.
    poll = issued[1]
    assert '[["Q3 pipeline review.xlsx", "Q3 pipeline review", "LibreOffice Calc"], 90.0]' in poll
    assert "time.sleep(1)" in poll


def test_the_window_a_document_gets_is_not_always_named_after_it():
    """Measured on the guest: evince titles its window with the file name, LibreOffice with
    "<name> - LibreOffice Calc", gedit with "<name> (~/Downloads) - gedit" -- and eog with
    "Image Viewer" and nothing else, because the file name lives in a client-side header bar the
    window manager never sees. Waiting only for the file name left eog polling its full 60
    seconds and then reporting a working viewer as not open."""
    catalog = desktop_apps()
    assert window_titles(catalog["evince"], "Invoice 4471.pdf")[0] == "Invoice 4471.pdf"
    assert "Image Viewer" in window_titles(catalog["image_viewer"], "social-frame.svg")

    windows = [{"id": "9", "name": "Image Viewer", "x": 310, "y": 60, "width": 728, "height": 690}]
    titles = window_titles(catalog["image_viewer"], "social-frame.svg")
    assert document_window(windows, titles)["id"] == "9"
    # And the report says the evidence was the application's name, not the document's.
    values = {"xdotool": True, "waited": 3.0, "windows": windows}
    opened = open_material(
        lambda command: json.dumps(values) if "getwindowname" in command else "",
        "Documents/Templates/social-frame.svg",
        maximise=False,
    )
    assert opened["matched_on"] == "application"


def test_the_sessions_own_full_screen_windows_are_not_modals():
    """Every screen on this image carries "mutter guard window" and "@!0,0;BDHF", both 1280x800.
    Not knowing them, the covering rule reported a modal over all six applications at once."""
    document = {"id": "1", "name": "x.pdf", "x": 70, "y": 27, "width": 1210, "height": 773}
    session = [
        {"id": "2", "name": "mutter guard window", "x": 0, "y": 0, "width": 1280, "height": 800},
        {"id": "3", "name": "@!0,0;BDHF", "x": 0, "y": 0, "width": 1280, "height": 800},
        {"id": "4", "name": "gnome-shell", "x": -200, "y": -200, "width": 1, "height": 1},
    ]
    assert covering_windows([document, *session], "x.pdf") == []


def test_an_application_left_open_is_closed_by_its_window_and_not_by_its_name():
    """The Image Viewer stayed open behind the next four applications: the close was a name
    search for the file, and eog's window is not named after the file it shows."""
    assert "windowclose 4194320" in close_command("4194320")


def test_open_material_refuses_a_format_no_application_opens():
    """An authoring mistake, raised where the author is still in the loop: naming a material
    `.dwg` is a decision, and a launcher that silently opened nothing would look like a slow app."""
    with pytest.raises(ValueError, match="no desktop application opens"):
        open_material(lambda command: "", "Desktop/site plan.dwg")


def test_a_material_lands_where_the_grader_looks_for_it():
    """The launcher and grader_core._staged_files must name the same file; documents.guest_path is
    the one rule, so a material outside the home folders goes to the Desktop for both."""
    assert guest_target("Documents/Q3 pipeline review.xlsx") == "/home/ga/Documents/Q3 pipeline review.xlsx"
    assert guest_target("notes.md") == "/home/ga/Desktop/notes.md"
    assert guest_target("/home/ga/Downloads/x.pdf") == "/home/ga/Downloads/x.pdf"


def test_readers_return_what_a_grader_asserts_on(tmp_path):
    """documents.py only writes. Without these a task can put work in a workbook and no check can
    read it back: the sheet's rows, the document's paragraphs and tables, the deck's slides."""
    book = tmp_path / "Q3 pipeline review.xlsx"
    book.write_bytes(render_material(book.name, WORKBOOK))
    sheets = read_material(book)
    assert sheets["kind"] == "sheets"
    assert list(sheets["sheets"]) == ["Q3 Pipeline", "Owners"]
    assert sheets["sheets"]["Q3 Pipeline"][1][:3] == [
        "Northwind Retail Group",
        "Priya Raghavan",
        "Negotiation",
    ]
    assert sheets["sheets"]["Q3 Pipeline"][1][3] == 184000  # a number stays a number for a cell check

    document = tmp_path / "Contact notes.docx"
    document.write_bytes(render_material(document.name, "# Clinic notes\n\nSpoke to Priya Raghavan.\n"))
    read = read_material(document)
    assert read["kind"] == "document" and "Priya Raghavan" in read["text"]
    assert "Clinic notes" in read["paragraphs"]

    deck = tmp_path / "Service positioning.pptx"
    deck.write_bytes(render_material(deck.name, "Service year\n- Northwind Retail Group\n---\nNext steps\n"))
    slides = read_material(deck)
    assert slides["kind"] == "slides" and len(slides["slides"]) == 2
    assert slides["slides"][0]["title"] == "Service year"
    assert "Northwind Retail Group" in slides["text"]

    invoice = tmp_path / "Invoice 4471.pdf"
    invoice.write_bytes(render_material(invoice.name, "# Sandstone Printer Care\nInvoice 4471\n"))
    pdf = read_material(invoice)
    assert pdf["kind"] == "pdf" and "Invoice 4471" in pdf["text"] and len(pdf["pages"]) == 1


def test_a_sheet_tab_is_part_of_what_the_worker_sees(tmp_path):
    """The seeding convention makes a sheet name a company word (`## Sheet: Requests`), and the tab
    is on screen at the bottom of the window. Left out of the flat text, 10 of 12 sampled workbooks
    read back as having lost a word that is plainly visible."""
    book = tmp_path / "register.xlsx"
    book.write_bytes(render_material(book.name, WORKBOOK))
    assert "Q3 Pipeline" in read_material(book)["text"]
    assert "Owners" in read_material(book)["text"]


def test_a_csv_reads_back_in_the_same_shape_as_a_workbook(tmp_path):
    """A .csv routed to Calc is still text on disk and openpyxl cannot open one. Returning the same
    {kind: sheets} shape means a grader writes one assertion for both."""
    path = tmp_path / "client contacts (1).csv"
    path.write_text("Name,Territory\nPriya Raghavan,Northeast\n")
    read = read_material(path)
    assert read["kind"] == "sheets"
    assert read["sheets"]["Sheet1"] == [["Name", "Territory"], ["Priya Raghavan", "Northeast"]]


def test_reading_a_format_the_contract_does_not_parse_says_so(tmp_path):
    """A silent empty read is the sweep's first defect class in miniature: a stage that answers a
    real verdict's receipt from nothing. Calc opens .ods; no reader here parses one, and a grader
    asking for a value out of it must be told rather than handed an empty string."""
    odf = tmp_path / "budget.ods"
    odf.write_bytes(b"PK\x03\x04not really")
    with pytest.raises(ValueError, match="does not parse"):
        read_material(odf)
    with pytest.raises(ValueError, match="no desktop application opens"):
        read_material(tmp_path / "site plan.dwg")


def test_a_world_already_on_disk_is_observed_and_never_failed(tmp_path):
    """The standing rule: never block on a defect the pipeline cannot repair. No stage rewrites a
    material's filename after seeding, so a material nothing opens is a warning, while a catalog
    that could not launch anything is an error raised where the author can still fix it."""
    (tmp_path / "site plan.dwg").write_text("x")
    (tmp_path / "budget.ods").write_bytes(b"PK\x03\x04")
    findings = check_materials(sorted(tmp_path.iterdir()))
    assert {finding["severity"] for finding in findings} == {"warning"}
    assert len(findings) == 2

    broken = {"gedit": {"name": "Text Editor", "packages": [], "launch": "gedit", "opens": [".txt"]}}
    broken["gedit"] |= {"reader": "telepathy", "ready_seconds": -1, "settle_seconds": 2}
    errors = check_desktop_catalog(broken)
    assert {finding["severity"] for finding in errors} == {"error"}
    messages = " ".join(finding["message"] for finding in errors)
    assert "{path}" in messages and "telepathy" in messages and "no package" in messages
    assert ".pdf" in messages  # documents.py renders it and this catalog opens nothing of the kind


def test_two_applications_may_not_claim_the_same_material():
    """Whichever won would depend on dict order, and the two would grade the file differently."""
    both = copy.deepcopy(desktop_apps())
    both["text_editor"]["opens"].append(".xlsx")
    messages = [finding["message"] for finding in check_desktop_catalog(both)]
    assert any(".xlsx is also opened by" in message for message in messages)


def test_a_workbook_opens_at_a_width_that_shows_its_values(tmp_path):
    """openpyxl stores no column widths, so Calc opened every seeded workbook at the 8.43-character
    default and clipped whatever was wider: a live guest showed "Northwir...", "Priya Rag...",
    "Negotiati...". Across the 266 workbooks on disk, 2,206 of 2,811 columns hold a value wider
    than the default and 6,621 of 14,171 values are clipped."""
    from openpyxl import load_workbook

    long_note = "A note that runs on for far longer than any column should ever be made to be." * 3
    book = tmp_path / "register.xlsx"
    book.write_bytes(render_material(book.name, WORKBOOK + f"## Sheet: Notes\nNote\n{long_note}\n"))
    loaded = load_workbook(book)
    pipeline_sheet = loaded["Q3 Pipeline"]
    assert pipeline_sheet.column_dimensions["A"].width >= len("Northwind Retail Group")
    assert pipeline_sheet.column_dimensions["B"].width >= len("Priya Raghavan")
    assert pipeline_sheet.column_dimensions["C"].width >= len("Negotiation")
    # A short column is not narrowed below what the format gives it anyway.
    assert loaded["Owners"].column_dimensions["B"].width >= DEFAULT_COLUMN_WIDTH
    # And a paragraph in a cell does not become a column wider than the window: past the cap Calc
    # spills the text over the empty cells beside it, which is what a person sees in a real sheet.
    assert loaded["Notes"].column_dimensions["A"].width == MAX_COLUMN_WIDTH


def test_material_values_takes_cells_before_rows():
    """interact_check.readable is imported rather than restated -- it is the rule that stopped a
    Chinese-language app reading as showing none of its records -- and a spreadsheet row has to
    contribute its values, not one comma-joined string that appears nowhere on screen."""
    values = material_values("Account,Owner,Stage\nNorthwind Retail Group,Priya Raghavan,Negotiation\n")
    assert "Northwind Retail Group" in values
    assert "Priya Raghavan" in values
    assert "184000" not in values  # an identifier or a number is not a value a person reads


def test_desktop_is_an_adapter_and_tells_an_author_what_the_desktop_can_do():
    """The author is judged by rules it can see: the surface's applications, the formats they open
    and the fact that grading reads the file back, all in the mounting facts the prompt carries."""
    assert "desktop" in ADAPTERS
    config = {"design": {"runtime_adapter": "desktop", "available_runtime_apps": ["libreoffice_calc"]}}
    facts = runtime_mounting(config)
    assert facts["adapter"] == "desktop"
    assert facts["apps"]["libreoffice_calc"]["opens"] == [".xlsx", ".xlsm", ".ods", ".csv"]
    assert facts["apps"]["libreoffice_calc"]["graded_from"] == "sheets"
    assert facts["unmapped_app_ids"] == []
    assert any("suffix one of the listed applications opens" in rule for rule in facts["requirements"])
    validate_runtime_mounting(["libreoffice_calc", "evince"], adapter="desktop")


def test_an_application_the_image_has_no_launcher_for_is_rejected():
    """The desktop analogue of the hub adapter's "not seedable through the hub contract": a surface
    naming an application nobody wrote a launch line for would fail at VM launch, silently."""
    with pytest.raises(ValueError, match="not launchable"):
        validate_desktop_apps(["libreoffice_calc", "photoshop"])
    with pytest.raises(ValueError, match="not launchable"):
        validate_runtime_mounting(["photoshop"], adapter="desktop")


def test_a_desktop_run_is_refused_before_any_artifact_when_its_surface_is_not_launchable(root, monkeypatch):
    """The pipeline check the hub adapter already has, at the same point: a desktop surface is
    validated against catalogs/desktop_apps.json rather than against apps.json, which knows
    nothing about desktop applications and would reject every one of them."""
    value = tomllib.loads((root / "config.toml").read_text())
    value["design"] = {"runtime_adapter": "desktop", "available_runtime_apps": ["gmail_mock"]}
    monkeypatch.setattr(pipeline, "load_config", lambda text: copy.deepcopy(value))
    monkeypatch.setattr(pipeline, "freeze_skills", lambda *a, **k: pytest.fail("reached run preparation"))
    with pytest.raises(ValueError, match="not launchable"):
        pipeline.new_run(root, 1, 1)
    assert not (root / "runs").exists()


# ------------------------------------------------------------------ gradeability


def _exports(tmp_path):
    documents = tmp_path / "w1/Documents"
    documents.mkdir(parents=True)
    return documents


def test_a_deck_can_carry_a_content_check_at_all(tmp_path):
    """Before slide_contains the grader had sheet_cell, pdf_contains and docx_contains and nothing
    for a deck, so the 71 seeded .pptx materials could carry no value check: any task whose work
    landed in a deck was ungradeable, however well Impress opened it."""
    documents = _exports(tmp_path)
    deck = documents / "positioning.pptx"
    deck.write_bytes(
        render_material(
            deck.name,
            "Service year\n- Northwind Retail Group renewed\n---\nNext steps\n- Close by 30 September\n",
        )
    )
    predicate = Predicate(operator="slide_contains", path="w1/Documents/positioning.pptx", text="renewed")
    assert not evaluate_predicate(predicate, {}, {})  # nothing exported yet
    assert evaluate_predicate(predicate, {}, {}, exports=tmp_path)
    # A title on a later slide counts too: a deck's readable surface is all of it.
    later = predicate.model_copy(update={"text": "Next steps"})
    assert evaluate_predicate(later, {}, {}, exports=tmp_path)
    assert not evaluate_predicate(
        predicate.model_copy(update={"text": "cancelled"}), {}, {}, exports=tmp_path
    )
    assert not evaluate_predicate(
        predicate.model_copy(update={"path": "w2/Documents/positioning.pptx"}), {}, {}, exports=tmp_path
    )
    deck.write_bytes(b"\xffbroken")
    with pytest.raises(ValueError, match="cannot read exported file"):
        evaluate_predicate(predicate, {}, {}, exports=tmp_path)


def test_a_workbook_can_be_checked_without_guessing_the_cell(tmp_path):
    """sheet_cell was the only workbook operator, and it needs the author to name the exact cell a
    worker will use. A row appended at the bottom, or a value one column over, fails a check that
    is otherwise right -- 266 workbooks on disk, and an author writing blind to the result."""
    documents = _exports(tmp_path)
    book = documents / "pipeline.xlsx"
    book.write_bytes(render_material(book.name, WORKBOOK))
    predicate = Predicate(
        operator="sheet_contains", path="w1/Documents/pipeline.xlsx", text="Cedar Ridge Logistics"
    )
    assert evaluate_predicate(predicate, {}, {}, exports=tmp_path)
    # The tab is part of what the worker sees, so it is part of what the check can name.
    assert evaluate_predicate(predicate.model_copy(update={"text": "Q3 Pipeline"}), {}, {}, exports=tmp_path)
    assert not evaluate_predicate(
        predicate.model_copy(update={"text": "Harbor Airport"}), {}, {}, exports=tmp_path
    )
    assert not evaluate_predicate(
        predicate.model_copy(update={"path": "w2/Documents/pipeline.xlsx"}), {}, {}, exports=tmp_path
    )


def test_a_workbook_is_read_both_ways_so_neither_kind_reads_empty(tmp_path):
    """A workbook has two answers and reading one of them is a silent miss, not a verdict. A file a
    script wrote carries the formula and no cached value; one an application saved carries the
    cached value. data_only=True alone answers "not there" for the first, False for the second."""
    from openpyxl import Workbook, load_workbook

    documents = _exports(tmp_path)
    book = Workbook()
    book.active.title = "Totals"
    book.active["A1"] = "=SUM(B1:B9)"  # never opened by Calc: no cached value exists
    book.save(documents / "totals.xlsx")
    assert load_workbook(documents / "totals.xlsx", data_only=True)["Totals"]["A1"].value is None
    formula = Predicate(operator="sheet_contains", path="w1/Documents/totals.xlsx", text="SUM(B1:B9)")
    assert evaluate_predicate(formula, {}, {}, exports=tmp_path)


def test_the_new_operators_are_value_checks_the_author_can_see_and_be_scored_on():
    """Defect class 5 in one line: an operator the skill never names is one the author never uses,
    and one the mechanical floor does not count is one a deck task can never clear. Both new
    operators pin a value the team has to produce, so both count."""
    assert {"slide_contains", "sheet_contains"} <= FILE_OPERATORS
    assert {"slide_contains", "sheet_contains"} <= VALUE_OPERATORS
    from company_envs.world.grader_author import AUTHOR_INSTRUCTIONS

    assert "slide_contains(path,text)" in AUTHOR_INSTRUCTIONS
    assert "sheet_contains(path,text)" in AUTHOR_INSTRUCTIONS
    skill = Path(__file__).resolve().parents[1] / ".agents/skills/company-task-grader/SKILL.md"
    assert "`slide_contains`" in skill.read_text() and "`sheet_contains`" in skill.read_text()


@pytest.mark.parametrize(
    "fields",
    [
        {"operator": "slide_contains", "path": "w1/Documents/*.pptx", "text": "x"},
        {"operator": "sheet_contains", "path": "w1/Documents/*.xlsx", "text": "x"},
        {"operator": "slide_contains", "path": "w1/Documents/a.pptx", "text": ""},
        {"operator": "sheet_contains", "path": "w1/Documents/a.xlsx"},
        {"operator": "sheet_contains", "path": "w1/Documents/a.xlsx", "text": "x", "sheet": "Q3"},
        {"operator": "slide_contains", "path": "w1/Documents/a.pptx", "text": "x", "value_json": '"x"'},
    ],
)
def test_the_new_operators_keep_the_rules_the_others_have(fields):
    """No exact path, no empty text, no sheet/cell, no operand: whatever the existing document
    operators refuse, these refuse the same way, so the author has one rule and not three."""
    with pytest.raises(ValueError):
        Predicate(**fields)


def test_the_kill_pattern_cannot_match_the_command_that_runs_it():
    """`pkill -9 -f blender` matches the command line running pkill, so the first live run killed
    its own shell, left Blender on screen, and the relaunch check then passed against an
    application that had never stopped -- an empty run writing a real verdict's receipt. The
    pattern's first character is bracketed, which matches the process and not the pattern."""
    line = kill_command({"kill": "blender"})
    assert "[b]lender" in line and "-f -- " in line
    import re as regex

    assert regex.search("[b]lender", "/usr/bin/blender --factory-startup")  # the app matches
    assert not regex.search("[b]lender", line)  # the command running it does not


def test_every_catalogued_application_says_how_it_is_killed():
    """The kill is half of "survives a kill and relaunch", and an entry without one silently
    skips that half. openshot's is a path fragment because a bare name matches its own helpers."""
    for app_id, app in desktop_apps().items():
        assert app.get("kill"), app_id
        assert kill_command(app).startswith("pkill -9 -f -- ")


def test_an_application_is_launchable_by_name_and_not_only_by_suffix():
    """A .png routes to the Image Viewer, so a check asking for GIMP on one silently proved the
    viewer. Naming the application is what makes a per-application proof mean what it says."""
    issued = []

    def run(command):
        issued.append(command)
        if "getwindowname" not in command:
            return ""
        return '{"xdotool": true, "waited": 2.0, "windows": []}'

    opened = open_material(run, "Pictures/harbor.png", app_id="gimp", maximise=False)
    assert opened["app_id"] == "gimp"
    assert "gimp" in issued[0]
    with pytest.raises(ValueError, match="not a desktop application"):
        open_material(run, "Pictures/harbor.png", app_id="photoshop")


def test_an_application_that_opens_a_folder_needs_a_home_placeholder():
    """Files opens the worker's Documents, so its line carries {home} and no {path}. Without the
    rule, either the catalog check rejects a correct entry or a launch opens the shell's cwd."""
    catalog = desktop_apps()
    assert "{home}" in catalog["files"]["launch"] and catalog["files"]["opens"] == []
    assert "/home/ga/Documents" in launch_command(catalog["files"], "Documents")
    broken = copy.deepcopy(catalog)
    broken["files"]["launch"] = "nautilus"
    messages = " ".join(f["message"] for f in check_desktop_catalog(broken))
    assert "{home}" in messages


def test_grading_is_answered_per_file_and_not_per_application():
    """The number that decides whether an application yields tasks. GIMP opens a .png, whose
    reader can report only size and format, and an .svg, which is markup and carries values a
    check can name; answering per application would have called all 6 real image materials
    ungradeable when every one of them is text. VLC and Blender leave nothing readable at all,
    and saying so plainly is the point -- an application that renders beautifully and supports no
    content check is the desktop equivalent of a web app with no typed-write path."""
    catalog = desktop_apps()
    assert grading_of(catalog["image_viewer"], "social-frame.svg") == "content"
    assert grading_of(catalog["gimp"], "harbor.png") == "properties"
    assert grading_of(catalog["openshot"], "cut.osp") == "content"
    assert grading_of(catalog["vlc"]) == "none"
    assert grading_of(catalog["blender"]) == "none"
    assert grading_of(catalog["libreoffice_calc"], "book.xlsx") == "content"


def test_every_cua_gym_desktop_type_the_image_carries_has_an_entry():
    """The coverage question, answerable from the catalogue rather than from a document. chrome is
    absent on purpose: hub_vm already ships Chrome for Testing per worker, and a second browser in
    the image would shadow the one the worker actually gets."""
    covered = {app.get("cua_gym_type") for app in desktop_apps().values()} - {None}
    assert covered == {
        "libreoffice_calc",
        "libreoffice_writer",
        "libreoffice_impress",
        "pdf",
        "vscode",
        "gimp",
        "vlc",
        "blender",
        "openshot",
        "os",
    }
    assert "chrome" not in covered


def test_an_openshot_project_is_json_and_so_can_carry_a_content_check(tmp_path):
    """The one media application that is gradeable: the .mp4 it edits leaves nothing to read, but
    the project it saves is JSON, so a check can name the clip, its position or the title."""
    project = tmp_path / "harbor cut.osp"
    project.write_text('{"clips": [{"title": "Harbor line walkthrough", "position": 0.0}]}')
    read = read_material(project)
    assert read["kind"] == "text" and "Harbor line walkthrough" in read["text"]


STUB_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="720" viewBox="0 0 1080 720">'
    "<title>Harbor abstract science illustration</title>"
    "<desc>Linked illustration, asset-harbor-science. Credit: Lacuna Illustration Office.</desc>"
    '<image href="https://assets.lakebridgeaudience.com/harbor/harbor-science.svg" x="0" y="0" '
    'width="1080" height="720"/></svg>'
)


def test_a_picture_material_reaches_the_guest_as_a_picture(tmp_path):
    """documents.py rendered .pdf/.docx/.xlsx/.pptx and passed a .png through as text, so a
    material whose name promised a picture arrived as bytes no viewer could open -- which is why
    GIMP had nothing in any world to open. Same move the four document formats already make."""
    picture = tmp_path / "harbor-science.png"
    picture.write_bytes(render_material(picture.name, STUB_SVG.encode()))
    read = read_material(picture)
    assert read["kind"] == "image" and read["format"] == "PNG"
    assert (read["width"], read["height"]) == (1080, 720)
    # Deterministic: a relaunch draws the same picture, so an unchanged desktop keeps its snapshot.
    assert picture.read_bytes() == render_material(picture.name, STUB_SVG.encode())
    assert render_material("shot.jpg", STUB_SVG.encode())[:3] == b"\xff\xd8\xff"


def test_an_svg_that_points_at_the_internet_is_made_drawable(tmp_path):
    """5 of the 6 image materials on disk are stubs whose only <image> points at
    assets.lakebridgeaudience.com. A worker VM runs QEMU restrict=on, so that fetch can never
    happen and both viewers open on an empty canvas. The company's own title is drawn instead."""
    drawn = render_material("harbor-science.svg", STUB_SVG.encode()).decode()
    assert "assets.lakebridgeaudience.com" not in drawn
    assert "Harbor abstract science illustration" in drawn
    assert "<title>" in drawn  # the company's own metadata is not thrown away
    # An SVG that already draws itself is the company's artwork and comes back untouched.
    own = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10" fill="#eee"/></svg>'
    assert render_material("frame.svg", own.encode()).decode() == own


def test_a_video_lands_in_the_videos_folder():
    """Videos was missing from HOME_FOLDERS, so Videos/clip.mp4 landed at Desktop/Videos/clip.mp4
    -- not where the guest's own sidebar points. No world ships media yet; the folder has to be
    right before one does."""
    assert guest_path("Videos/clip.mp4") == "Videos/clip.mp4"
    assert guest_path("Pictures/logo.png") == "Pictures/logo.png"
    assert guest_path("Elsewhere/notes.txt") == "Desktop/Elsewhere/notes.txt"


def test_every_folder_a_material_can_be_seeded_in_is_one_a_grader_can_read():
    """Pictures, Music and Videos were places a material could be seeded and neither exported out
    of a guest (mypcbench.download_desktop) nor accepted by a file predicate
    (grader_core.Predicate): opened by an application, invisible to every check. Both halves now
    read documents.GRADED_FOLDERS, so the next folder cannot be added to one side only."""
    import inspect

    from company_envs.world.backends import mypcbench
    from company_envs.world.grader_core import Predicate

    assert set(GRADED_FOLDERS) == set(HOME_FOLDERS)
    for folder in HOME_FOLDERS:
        Predicate(operator="file_exists", path=f"w1/{folder}/report.pdf")
    with pytest.raises(ValueError, match="plus a filename"):
        Predicate(operator="file_exists", path="w1/private/report.pdf")
    # The export reads the same list rather than restating it.
    export = inspect.getsource(mypcbench.SSHTransport.download_desktop)
    assert "GRADED_FOLDERS" in export
    assert "Desktop Documents Downloads" not in export


def test_rendering_an_already_rendered_material_returns_it_untouched():
    """The guard covered %PDF and PK only, so a second pass over a PNG treated its own bytes as
    text and drew "PNG IHDR IDAT" on a canvas -- which is what GIMP opened on a live guest.
    Every format documents.py writes has to survive being handed back to it."""
    for name, source in (
        ("harbor.png", STUB_SVG.encode()),
        ("harbor.jpg", STUB_SVG.encode()),
        ("harbor.svg", STUB_SVG.encode()),
        ("register.xlsx", WORKBOOK.encode()),
        ("notes.docx", b"# Clinic notes"),
        ("deck.pptx", b"Service year"),
        ("invoice.pdf", b"# Sandstone Printer Care"),
    ):
        once = render_material(name, source)
        assert render_material(name, once) == once, name


def _exported(tmp_path, worker="w1"):
    root = tmp_path / "exports"
    for folder in ("Desktop", "Documents", "Downloads", "Pictures"):
        (root / worker / folder).mkdir(parents=True, exist_ok=True)
    return root


def test_a_move_can_be_checked_on_both_halves_or_a_copy_passes_for_it(tmp_path):
    """Files (nautilus) had no operator that could see its work. The ordinary task -- file the
    scan into the right folder under the right name -- is filesystem state, which the export
    already returns; what was missing was the half that says it is no longer where it was.
    file_exists alone passes for a copy when a move was asked for."""
    before, after = _exported(tmp_path / "before"), _exported(tmp_path / "after")
    (before / "w1/Downloads/scan_0021.pdf").write_bytes(render_material("scan.pdf", b"# Invoice 4471"))
    (after / "w1/Documents/Invoice 4471.pdf").write_bytes(render_material("i.pdf", b"# Invoice 4471"))
    gone = Predicate(operator="file_absent", path="w1/Downloads/scan_*.pdf")
    arrived = Predicate(operator="file_exists", path="w1/Documents/Invoice 4471.pdf")
    # Fires on the work and not without it, which is the whole of what a check has to do.
    assert not evaluate_predicate(gone, {}, {}, exports=before)
    assert not evaluate_predicate(arrived, {}, {}, exports=before)
    assert evaluate_predicate(gone, {}, {}, exports=after)
    assert evaluate_predicate(arrived, {}, {}, exports=after)
    # A copy leaves the original: the half that catches it is the half that was missing.
    copied = _exported(tmp_path / "copied")
    (copied / "w1/Downloads/scan_0021.pdf").write_bytes(b"x")
    (copied / "w1/Documents/Invoice 4471.pdf").write_bytes(b"x")
    assert evaluate_predicate(arrived, {}, {}, exports=copied)
    assert not evaluate_predicate(gone, {}, {}, exports=copied)


def test_file_absent_refuses_to_answer_when_nothing_was_exported(tmp_path):
    """An empty export is not proof that a file is gone; it is proof of nothing. Answering True
    would be a check that passes hardest when the episode produced least -- the same defect as a
    receipt that always says done."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no files were exported"):
        evaluate_predicate(Predicate(operator="file_absent", path="w1/Downloads/*"), {}, {}, exports=empty)


def test_a_picture_can_be_checked_on_the_one_thing_a_brief_can_fix(tmp_path):
    """GIMP could be read for size and format and no grader operator could assert either, so it
    carried no task at all. image_size is exact -- no tolerance to argue about -- and is what a
    crop, a resize or an export at a fixed size produces."""
    before, after = _exported(tmp_path / "before"), _exported(tmp_path / "after")
    from PIL import Image

    for root, size in ((before, (1080, 720)), (after, (1200, 628))):
        Image.new("RGB", size, (200, 200, 200)).save(root / "w1/Pictures/banner.png")
    wanted = Predicate(operator="image_size", path="w1/Pictures/banner.png", value_json='"1200x628"')
    assert not evaluate_predicate(wanted, {}, {}, exports=before)
    assert evaluate_predicate(wanted, {}, {}, exports=after)
    with pytest.raises(ValueError, match="cannot read exported file"):
        (after / "w1/Pictures/banner.png").write_bytes(b"not a picture")
        evaluate_predicate(wanted, {}, {}, exports=after)


def test_the_new_file_operators_are_scored_the_way_they_deserve():
    """image_size pins a value the team has to produce, so it counts toward the mechanical floor.
    file_absent pins a shape -- deleting everything clears it -- so it does not, the same
    judgement file_exists and count_gte already get."""
    from company_envs.world.grader_author import COUNT_OPERATORS, VALUE_OPERATORS

    assert {"image_size", "file_absent"} <= FILE_OPERATORS
    assert "image_size" in VALUE_OPERATORS
    assert "file_absent" in COUNT_OPERATORS and "file_absent" not in VALUE_OPERATORS


def test_typing_is_described_only_for_applications_a_worker_types_into():
    """A probe that typed into VLC and reported success would be measuring the absence of a crash.
    LibreOffice's save is ctrl+s *and* the Return that answers Keep current format, which it
    raises for every non-ODF save -- a check sending only ctrl+s screenshots the modal and calls
    it saved."""
    catalog = desktop_apps()
    assert type_commands(catalog["vlc"], "probe") == []
    assert type_commands(catalog["files"], "probe") == []
    calc = " ".join(type_commands(catalog["libreoffice_calc"], "probe 1"))
    assert "xdotool type" in calc and "ctrl+s" in calc
    assert calc.count("Return") == 2  # one commits the cell, one answers the format dialog
    assert "ctrl+s" in " ".join(type_commands(catalog["text_editor"], "probe 1"))
