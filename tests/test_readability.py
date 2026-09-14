"""Offline readability coverage, register calibration, and CORT fixture regression."""

import json
import shutil
from pathlib import Path

import pytest

from company_envs.world.readability import (
    calibration_examples,
    readability_report,
    write_readability_report,
)


def _write(folder, relative, value):
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def cort(tmp_path):
    source = Path(__file__).resolve().parents[1] / "companies/cort"
    folder = tmp_path / "companies/cort"
    shutil.copytree(source / "world.v1-compact", folder / "world")
    shutil.copytree(source / "tasks", folder / "tasks")
    return folder


def test_cort_export_is_flagged_briefs_are_plain_and_reports_are_written(cort):
    report = write_readability_report(cort)
    categories = report["categories"]
    assert categories["brief"]["verdict"] == "plain"
    assert categories["brief"]["item_count"] == len(list((cort / "tasks").glob("*/assignment.json")))
    assert categories["materials"]["item_count"] == sum(
        p.is_file() for p in (cort / "world/materials").rglob("*")
    )
    export = next(
        item
        for item in categories["materials"]["items"]
        if item["source"] == "world/materials/w2/exports/matching-stock-scope.txt"
    )
    assert export["verdict"] == "system-log"
    assert export["jargon_density"] > 6
    assert export["word_count"] > 40
    assert export["sample_sentence"]
    assert any("reads like a system log" in reason for reason in export["reasons"])
    assert categories["materials"]["verdict"] == "system-log"
    assert categories["messages"]["item_count"] > 0
    assert len(report["summary"].split()) < 180
    assert json.loads((cort / "REPORT-readability.json").read_text()) == report
    markdown = (cort / "REPORT-readability.md").read_text()
    assert "matching-stock-scope.txt" in markdown
    assert "## Brief: plain" in markdown
    assert "Words/sentence" in markdown
    assert "Codes/100 words" in markdown
    assert readability_report(cort) == report  # Generated reports are not inputs.


def test_definitions_in_materials_and_briefs_apply_company_wide(tmp_path):
    _write(
        tmp_path,
        "tasks/one/assignment.json",
        {
            "brief": "Check the ETA and SLA with the AR team. Review the purchase order (PO) and unexplained WIP."
        },
    )
    _write(tmp_path, "tasks/two/assignment.json", {"brief": "Please check the PO."})
    material = tmp_path / "world/materials/w1/glossary.md"
    material.parent.mkdir(parents=True)
    material.write_text(
        "ETA (estimated time of arrival). Service level agreement (SLA).\n"
        "Accounts receivable (AR). JavaScript Object Notation (JSON).\n"
        "Bill of materials (BOM). REF-17 is a reference. WIP (17) is not a definition."
    )
    _write(
        tmp_path, "world/mail.state.json", {"body": "ETA SLA AR PO WIP", "notes": "Work in progress (WIP)."}
    )
    report = readability_report(tmp_path)
    assert set(report["defined_acronyms"]) == {"ETA", "SLA", "AR", "PO", "JSON", "BOM"}
    brief = report["categories"]["brief"]["items"][0]
    assert brief["undefined_acronyms"] == ["WIP"]
    assert report["categories"]["brief"]["items"][1]["undefined_acronyms"] == []
    body = report["categories"]["messages"]["items"][0]
    assert body["undefined_acronyms"] == ["WIP"]  # Definitions in messages do not count.


def test_every_message_field_has_a_location_even_without_ids_or_eight_words(tmp_path):
    _write(
        tmp_path,
        "world/chat.state.json",
        {
            "id": "not prose",
            "title": "IGNORE THIS METADATA",
            "created_at": "2026-09-07",
            "comments": [{"body": "Please call Jo."}, {"body": "REQ-42 ACK; ROUTE-09 PENDING."}],
            "description": "Two trucks are available. Please call Jo before noon.",
            "notes": ["Call tomorrow.", ""],
            "email": {"html_body": "<p>Hello <b>Mira</b>.</p><p>We can help &amp; collect Friday.</p>"},
            "a/b~c": {"message": "Thanks!"},
        },
    )
    report = readability_report(tmp_path)
    category = report["categories"]["messages"]
    assert category["item_count"] == 7
    assert category["verdict"] == "system-log"  # Plain neighbors cannot dilute a bad message.
    items = {item["source"].split("#", 1)[1]: item for item in category["items"]}
    assert set(items) == {
        "/comments/0/body",
        "/comments/1/body",
        "/description",
        "/notes/0",
        "/notes/1",
        "/email/html_body",
        "/a~1b~0c/message",
    }
    assert items["/comments/0/body"]["verdict"] == "plain"
    assert items["/comments/1/body"]["verdict"] == "system-log"
    description = items["/description"]
    assert (description["word_count"], description["sentences"], description["average_sentence_length"]) == (
        9,
        2,
        4.5,
    )
    assert description["jargon_density"] == 0
    assert items["/email/html_body"]["word_count"] == 8
    assert "<" not in items["/email/html_body"]["sample_sentence"]
    assert items["/notes/1"]["sentences"] == 0
    assert items["/notes/1"]["average_sentence_length"] == 0


def test_a_category_is_judged_by_how_much_of_it_reads_badly():
    """Worst-item made the verdict a function of corpus size, not of writing.

    Applied to roughly 8,600 messages it made 44 of 48 worlds "system-log" and refused 19 of the
    21 that had passed review and the mechanical checks. Los Angeles County failed on 20 flagged
    items out of 12,190. One bad message in ten thousand is a message, not a world.
    """
    from company_envs.world.readability import _category_verdict

    def items(total, logs=0, dense=0):
        rows = [{"verdict": "system-log"}] * logs + [{"verdict": "dense"}] * dense
        return rows + [{"verdict": "plain"}] * (total - len(rows))

    assert _category_verdict(items(10_000, logs=20)) == "plain", "20 in 10,000 is not a world"
    assert _category_verdict(items(1_000, logs=60)) == "system-log", "6% is the corpus, not an item"
    assert _category_verdict(items(1_000, dense=40)) == "dense"
    assert _category_verdict([]) == "unmeasured"

    # A handful of items has no meaningful share, so the worst one still decides: a brief is one
    # item and a bad brief must fail.
    assert _category_verdict(items(1, logs=1)) == "system-log"
    assert _category_verdict(items(5, dense=1)) == "dense"


def test_thresholds_are_strict_and_single_reference_does_not_make_a_log(tmp_path):
    _write(
        tmp_path,
        "world/chat.state.json",
        {
            "messages": [
                {"body": " ".join(["word"] * 25) + "."},
                {"body": " ".join(["word"] * 26) + "."},
                {"body": "Check REQ-42 please."},
                {"body": " ".join(["REQ-42"] * 3 + ["word"] * 47)},  # Exactly six codes/100.
                {"body": " ".join(["REQ-42"] * 4 + ["word"] * 46)},
            ]
        },
    )
    items = readability_report(tmp_path)["categories"]["messages"]["items"]
    assert [item["verdict"] for item in items] == ["plain", "dense", "dense", "dense", "system-log"]
    assert items[3]["jargon_density"] == 6
    assert items[4]["jargon_density"] == 8


def test_missing_categories_and_binary_materials_are_explicit_coverage_gaps(tmp_path):
    report = readability_report(tmp_path)
    assert len(report["coverage_warnings"]) == 3
    assert all(category["item_count"] == 0 for category in report["categories"].values())
    material = tmp_path / "world/materials/w1/scan.pdf"
    material.parent.mkdir(parents=True)
    material.write_bytes(b"%PDF\x00\xff")
    report = write_readability_report(tmp_path)
    category = report["categories"]["materials"]
    assert category["item_count"] == 1
    assert category["measured_count"] == 0
    assert category["verdict"] == "dense"
    assert category["items"][0]["word_count"] is None
    assert category["items"][0]["status"] == "unreadable"
    assert "could not be measured" in report["summary"]


def test_calibration_examples_have_expected_registers(tmp_path):
    examples = calibration_examples()
    assert set(examples) == {"brief", "colleague_message", "customer_message", "onboarding_note"}
    for pair in examples.values():
        assert set(pair) == {"good", "bad"}
    _write(
        tmp_path,
        "world/chat.state.json",
        {"messages": [{"body": pair[quality]} for pair in examples.values() for quality in ("good", "bad")]},
    )
    items = readability_report(tmp_path)["categories"]["messages"]["items"]
    assert [item["verdict"] for item in items] == ["plain", "system-log"] * 4


def test_invalid_assignment_is_not_silently_skipped(tmp_path):
    _write(tmp_path, "tasks/one/assignment.json", {"brief": None})
    with pytest.raises(TypeError, match="brief must be a string"):
        readability_report(tmp_path)


def test_markdown_escapes_sample_table_delimiters(tmp_path):
    _write(tmp_path, "world/chat.state.json", {"body": "Call Jo | then call Mira."})
    write_readability_report(tmp_path)
    assert "Call Jo &#124; then call Mira." in (tmp_path / "REPORT-readability.md").read_text()


def test_declared_proper_names_are_not_codes_or_undefined_acronyms(tmp_path):
    _write(tmp_path, "company.json", {"name": "CORT"})
    _write(
        tmp_path,
        "world/chat.state.json",
        {
            "organizations": [{"id": 1, "name": "IKEA"}],
            "messages": [
                {"body": "CORT can help IKEA furnish the office. Please call CORT after speaking with IKEA."}
            ],
        },
    )
    item = readability_report(tmp_path)["categories"]["messages"]["items"][0]
    assert item["verdict"] == "plain"
    assert item["undefined_acronyms"] == [] and item["code_count"] == 0


def test_tabular_files_and_common_acronyms_do_not_read_as_system_logs():
    from company_envs.world.readability import _measure

    table = "Version,Effective,Service,USD per block,Status\n" + "\n".join(
        f"V{n},2026-04-0{n},SVC-{n},USD 90,ACTIVE" for n in range(1, 8)
    )
    item = _measure("world/materials/x/Documents/rates.xlsx", table, set())
    assert item["verdict"] != "system-log" and "USD" not in item["undefined_acronyms"]
    prose = _measure(
        "world/materials/x/Documents/note.docx", "The visit is at 10:00 EDT. Costs are in USD.", set()
    )
    assert prose["undefined_acronyms"] == []


def test_a_report_names_the_files_it_measured_so_it_can_be_invalidated(tmp_path):
    """The gate reported on worlds that no longer exist and could never be invalidated.

    cresa's REPORT-readability.json describes 8,442 messages in a world that is not on disk: the
    world was moved aside to world.old-instructions-20260908b. The driver computed freshness from
    world/BULK.json, tasks/_mined/MINE.json and this module's mtime -- none of which the report
    reads, and two of which cresa does not have -- so 13 of the 48 reports on disk predate a file
    they measured while that rule caught 11. A stat per measured file is the whole cost.
    """
    from company_envs.world.readability import stale_reasons

    _write(tmp_path, "tasks/t1/assignment.json", {"brief": "Extend the customer's sofa rental."})
    _write(tmp_path, "world/chat.state.json", {"messages": [{"body": "Hi Jo, can we collect on Friday?"}]})
    report = readability_report(tmp_path)
    assert set(report["inputs"]) == {"tasks/t1/assignment.json", "world/chat.state.json"}
    assert stale_reasons(tmp_path, report) == []

    moved = tmp_path / "world.set-aside"
    (tmp_path / "world").rename(moved)
    assert stale_reasons(tmp_path, report) == ["world/chat.state.json was measured and is no longer on disk."]
    # And a report that measured nothing does not read as a pass.
    empty = readability_report(tmp_path)
    assert empty["categories"]["messages"]["verdict"] == "unmeasured"
    assert empty["categories"]["materials"]["verdict"] == "unmeasured"

    moved.rename(tmp_path / "world")
    _write(tmp_path, "world/chat.state.json", {"messages": [{"body": "Hi Jo, can we collect on Monday?"}]})
    assert stale_reasons(tmp_path, report) == ["world/chat.state.json changed since it was measured."]
    _write(tmp_path, "world/extra.state.json", {"messages": [{"body": "One more."}]})
    assert "world/extra.state.json is on disk and was never measured." in stale_reasons(tmp_path, report)
    assert stale_reasons(tmp_path, {"summary": "an older report"}) == [
        "The report does not record which files it measured."
    ]


def test_only_the_brief_is_reported_as_blocking(tmp_path):
    """A gate must not block on a defect the pipeline cannot repair.

    45 of the 48 reports on disk fail the agreement condition as written, and under today's share
    rule 17 still fail -- 14 of them on materials alone. world_repair calls a material "a worker's
    desktop file, not an app record" and leaves it; check_plain_language only ever emits warnings,
    so a message finding cannot reach the repair loop; and nothing hands this report to any author.
    The brief is the one prose the pipeline rewrites, and the whole cohort of 48 carries exactly one
    flagged brief, so blocking on briefs costs nothing and is repairable.
    """
    _write(tmp_path, "tasks/t1/assignment.json", {"brief": "Extend the customer's sofa rental."})
    _write(
        tmp_path,
        "world/chat.state.json",
        {"messages": [{"body": f"REQ-{n} ACK; AVL-17 PASS; ROUTE-09 PENDING; ETA TBD."} for n in range(20)]},
    )
    report = readability_report(tmp_path)
    assert report["categories"]["messages"]["verdict"] == "system-log"
    assert report["blocking"] == [] and report["advisory"] == ["messages"]
    assert "Advisory only (messages)" in report["summary"]

    _write(tmp_path, "tasks/t1/assignment.json", {"brief": "Reconcile EXT-42 against AVL-17 and SLA gates."})
    report = readability_report(tmp_path)
    assert report["blocking"] == ["brief"] and "Blocking: brief." in report["summary"]
