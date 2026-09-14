"""Offline review-sheet projections; no models, app servers, VMs or npm."""

import json
import shutil
from pathlib import Path

import pytest

from company_envs.world.readability import write_readability_report
from company_envs.world.review_sheet import write_cohort_sheet, write_review_sheet


def _write(folder, relative, value):
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def cort(tmp_path):
    source = Path(__file__).resolve().parents[1] / "companies/cort"
    folder = tmp_path / "companies/cort"
    # The live world can be mid-seed; use the finished compact snapshot exclusively.
    shutil.copytree(source / "world.v1-compact", folder / "world")
    shutil.copytree(source / "tasks", folder / "tasks")
    for name in ("company.json", "apps.json", "MANIFEST.json", "review.json"):
        shutil.copy2(source / name, folder / name)
    write_readability_report(folder)
    return folder


def test_compact_cort_sheet_has_roster_counts_and_exact_public_brief(cort):
    before = {p: p.read_bytes() for p in cort.rglob("*") if p.is_file()}
    output = write_review_sheet(cort)
    sheet = output.read_text()
    assert output == cort / "REVIEW-SHEET.md"
    assert len(sheet.splitlines()) < 120
    assert "Hearthway Furniture Rental" in sheet
    assert "Real analogue: CORT" in sheet
    assert "Real Estate and Rental and Leasing" in sheet
    assert "Raleigh–Durham" in sheet
    assert "Residential Rental Consultant" in sheet
    assert "Asset Reuse and Outlet Operations" in sheet
    assert "Rental Service and Delivery Coordination" in sheet
    for worker in range(1, 7):
        assert f"| w{worker} |" in sheet
    for path in (cort / "world").glob("*.state.json"):
        state = json.loads(path.read_text())
        assert path.name.removesuffix(".state.json") in sheet
        for key, value in state.items():
            if isinstance(value, list):
                assert f"{key}: {len(value)}" in sheet
    brief = json.loads((cort / "tasks/cort_o3/assignment.json").read_text())["brief"]
    assert brief in sheet
    assert "Feature cell: not yet; boss: not yet" in sheet
    assert "recorded calibration: not yet" in sheet
    assert "seeded_readback_verified" in sheet
    assert "materials: system-log" in sheet
    assert "Worst readability sample" in sheet
    assert sheet.count("[ ] Yes / [ ] No") == 5
    assert "Would three people really be needed?" in sheet
    assert before == {p: p.read_bytes() for p in before}
    assert write_review_sheet(cort).read_text() == sheet


def test_reports_latest_review_worst_sample_and_recorded_calibration(cort):
    _write(cort, "world/CHECKS.json", {"ok": False, "errors": 2, "warnings": 7})
    findings = [
        {"severity": "warning", "target": "world", "issue": f"Current issue {i}", "evidence": f"r{i}"}
        for i in range(6)
    ]
    findings.append({"severity": "error", "target": "mail", "issue": "Broken link", "evidence": "r9"})
    _write(
        cort,
        "world/REVIEW.json",
        {
            "final_verdict": "revise",
            "rounds": [
                {"verdict": {"verdict": "reject", "findings": [{"issue": "Old resolved issue"}]}},
                {"verdict": {"verdict": "revise", "findings": findings}},
            ],
        },
    )
    _write(
        cort,
        "tasks/cort_o3/workflow.json",
        {
            "manager_id": "w3",
            "feature_cell": {"app_id": "Zendesk_mock", "collections": ["tickets"]},
            "brief": "PRIVATE WORKFLOW BRIEF",
            "completion": "PRIVATE ANSWER",
        },
    )
    _write(
        cort,
        "runtime/grades/cort_o3/calibration.json",
        {
            "accepted": True,
            "scope": "state_checks_only",
            "reference_checks": ["PRIVATE GRADING KEY"],
        },
    )
    _write(
        cort,
        "runtime/tasks/cort_o3/CONTROLLER.json",
        {
            "status": "failed",
            "task_id": "cort_o3",
            "responsible_step": "launch",
            "options": {"dry_run": True},
        },
    )
    _write(
        cort,
        "REPORT-readability.json",
        {
            "categories": {
                "brief": {
                    "verdict": "dense",
                    "item_count": 1,
                    "flagged_count": 1,
                    "items": [
                        {
                            "verdict": "dense",
                            "sample_sentence": "Less severe sample.",
                            "source": "brief",
                            "jargon_density": 99,
                        },
                    ],
                },
                "messages": {
                    "verdict": "system-log",
                    "item_count": 1,
                    "flagged_count": 1,
                    "items": [
                        {
                            "verdict": "system-log",
                            "sample_sentence": "Worst | sample.",
                            "source": "mail#/body",
                            "jargon_density": 8,
                        },
                    ],
                },
            }
        },
    )
    sheet = write_review_sheet(cort).read_text()
    assert "world review: revise" in sheet
    assert "2 errors, 7 warnings (world/CHECKS.json)" in sheet
    assert sheet.count("- Review finding [") == 5
    assert "Broken link" in sheet
    assert "2 more findings" in sheet
    assert "Old resolved issue" not in sheet
    assert "Current issue 5" not in sheet
    assert "Feature cell: app_id: Zendesk_mock; collections: tickets; boss: w3" in sheet
    assert "recorded calibration: accepted (scope: state_checks_only)" in sheet
    assert "Controller: failed; task: cort_o3; responsible step: launch; dry run" in sheet
    assert "Worst readability sample (mail#/body): Worst \\| sample." in sheet
    assert "Less severe sample" not in sheet
    assert "PRIVATE" not in sheet
    assert len(sheet.splitlines()) < 120


def test_cohort_two_folders_counts_calibrations_and_leaves_decisions_blank(cort):
    second = cort.parent / "zeta"
    _write(second, "company.json", {"name": "Second | Company", "sector": "Transport\nServices"})
    _write(second, "tasks/two/assignment.json", {"brief": "Ship the order."})
    _write(second, "tasks/three/assignment.json", {"brief": "Check the receipt."})
    _write(second, "runtime/grades/two/calibration.json", {"accepted": True, "scope": "state_checks_only"})
    _write(second, "runtime/grades/three/calibration.json", {"accepted": False, "scope": "state_checks_only"})
    _write(second, "world/SEED.json", {"status": "seeded_review_failed", "review": "reject"})
    (cort.parent / "notes.txt").write_text("Not a company")
    output = write_cohort_sheet(cort.parent, cort.parent / "reports.md")
    sheet = output.read_text()
    rows = [line for line in sheet.splitlines() if line.startswith("| ")]
    assert len(rows) == 4  # Header, separator and exactly two companies.
    assert "Hearthway Furniture Rental (cort)" in rows[2]
    assert "cort_o3" in rows[2]
    assert "0/1" in rows[2]
    assert "Second \\| Company (zeta)" in rows[3]
    assert "Transport Services" in rows[3]
    assert "1/2" in rows[3]
    assert "seeded_review_failed | reject" in rows[3]
    assert all(line.endswith("|  |") for line in rows[2:])
    assert not (cort / "REVIEW-SHEET.md").exists()


def test_missing_inputs_do_not_invent_success_or_infer_boss(tmp_path):
    folder = tmp_path / "companies/empty"
    sheet = write_review_sheet(folder).read_text()
    assert "# Review sheet: empty" in sheet
    assert "Seed: not yet; world review: not yet" in sheet
    assert "Mechanical checks: not yet" in sheet
    assert "Readability: not yet" in sheet
    assert "Review findings: not yet" in sheet
    assert "Controller: not yet" in sheet
    assert len(sheet.splitlines()) < 120
    _write(folder, "tasks/draft/workflow.json", {"brief": "Private fallback", "worker_ids": ["w1"]})
    sheet = write_review_sheet(folder).read_text()
    assert "boss: not yet" in sheet
    assert "Private fallback" not in sheet
    output = write_cohort_sheet(folder.parent, tmp_path / "out/cohort.md")
    assert "empty (empty) | not yet" in output.read_text()


def test_top_level_counts_exclude_scalar_profiles_and_nested_rows(tmp_path):
    _write(tmp_path, "apps.json", {"apps": [{"app_id": "missing"}]})
    _write(
        tmp_path,
        "world/mail.state.json",
        {
            "users": [{"id": 1}],
            "tickets": {"t1": {"comments": [{}, {}]}},
            "currentUser": {"id": 1, "name": "Ada"},
            "zoom": 100,
            "empty": [],
        },
    )
    sheet = write_review_sheet(tmp_path).read_text()
    assert "| mail | users: 1; tickets: 1; empty: 0 |" in sheet
    assert "| missing | not yet |" in sheet
    assert "currentUser:" not in sheet
    assert "comments:" not in sheet


def test_multiline_brief_is_verbatim_even_with_markdown_fences(tmp_path):
    brief = "  First | line.\n\n```\nKeep *exact* punctuation.\n```\nLast line.  "
    _write(tmp_path, "tasks/task/assignment.json", {"brief": brief})
    sheet = write_review_sheet(tmp_path).read_text()
    assert f"````text\n{brief}\n````" in sheet


def test_check_fallbacks_and_rejected_calibration(tmp_path):
    _write(
        tmp_path, "world/SEED.json", {"review": "accept", "mechanical_checks": {"errors": 0, "warnings": 4}}
    )
    _write(tmp_path, "tasks/task/assignment.json", {"brief": "Public brief"})
    _write(
        tmp_path, "runtime/grades/task/calibration.json", {"accepted": False, "scope": "state_checks_only"}
    )
    sheet = write_review_sheet(tmp_path).read_text()
    assert "0 errors, 4 warnings (world/SEED.json)" in sheet
    assert "recorded calibration: rejected" in sheet
    _write(tmp_path, "runtime/CHECKS.json", {"errors": 1, "warnings": 0})
    assert "1 errors, 0 warnings (runtime/CHECKS.json)" in write_review_sheet(tmp_path).read_text()


def test_oversized_brief_does_not_silently_truncate_or_overwrite_sheet(tmp_path):
    output = write_review_sheet(tmp_path)
    original = output.read_bytes()
    _write(tmp_path, "tasks/long/assignment.json", {"brief": "Important line\n" * 120})
    with pytest.raises(ValueError, match="not truncated"):
        write_review_sheet(tmp_path)
    assert output.read_bytes() == original


def test_a_dead_company_a_blocked_one_and_one_queued_for_tonight_are_different_rows(tmp_path):
    """Every report collapsed the whole fleet into one word, so a stuck company was invisible.

    With 1 of 99 companies reaching agreement, 97 of the 98 others came out of the cohort report as
    ``first_failing_condition: "seeded"`` -- 14 dead after three seed attempts, 23 blocked with
    their one review repair already recorded, 24 awaiting a first seed, and one (cort) awaiting
    assign-apps all rendered identically. The driver's own discriminators are on disk and no report
    read them.
    """
    from company_envs.world.review_sheet import company_stage

    companies = tmp_path / "companies"
    for name in ("dead", "blocked", "queued", "no-apps", "flying", "finished"):
        (companies / name / "world").mkdir(parents=True)
    _write(companies / "dead", "world/SEED_FAILED.json", {"reason": "three attempts", "attempts": 3})
    _write(companies / "blocked", "world/SEED.json", {"status": "seeded_review_failed"})
    _write(companies / "blocked", "world/REVIEW.json", {"final_verdict": "reject"})
    _write(companies / "blocked", "world/REVIEW-REPAIR.json", {"repaired": 2})
    _write(companies / "queued", "tasks/_plain_brief/REWRITE.json", {"rewritten": 1})
    _write(companies / "queued", "tasks/_worker_apps/ASSIGN.json", {"workers": 3})
    _write(companies / "flying", "world/SEED.json", {"status": "seeded_reviewed"})
    _write(companies / "finished", "AGREEMENT.json", {"done": True})

    stages = {name: company_stage(companies / name) for name in sorted(p.name for p in companies.iterdir())}
    assert {name: stage["stage"] for name, stage in stages.items()} == {
        "dead": "seed_failed",
        "blocked": "review_not_accepted",
        "queued": "awaiting_seed",
        "no-apps": "awaiting_apps",
        "flying": "in_flight",
        "finished": "done",
    }
    assert stages["dead"]["repair_recorded"] and stages["blocked"]["repair_recorded"]
    assert not stages["queued"]["repair_recorded"]
    assert stages["queued"]["waiting_on"] == "world/SEED.json"
    assert stages["no-apps"]["waiting_on"] == "tasks/_plain_brief/REWRITE.json"

    # A world moved aside is not a world: cort and cresa both keep one under another name, and a
    # report that counted the directory would call them seeded.
    (companies / "queued" / "world.v1-compact").mkdir()
    aside = company_stage(companies / "queued")
    assert aside["world_on_disk"] is False and aside["worlds_set_aside"] == ["world.v1-compact"]

    sheet = write_cohort_sheet(companies, tmp_path / "COHORT-SHEET.md").read_text()
    assert "seed_failed (repair recorded)" in sheet
    assert "review_not_accepted (repair recorded)" in sheet
    assert "awaiting_seed (world set aside: world.v1-compact)" in sheet
    assert "| Position |" in sheet


def test_writing_a_sheet_somewhere_else_does_not_advance_the_company(tmp_path):
    """REVIEW-SHEET.md is the driver's stage-18 marker as well as a human's reading.

    Reading a company by writing its sheet into the company moved its apparent position: cort
    carries a REVIEW-SHEET.md while it is still waiting for assign-apps at stage 2.
    """
    folder = tmp_path / "companies/acme"
    _write(folder, "company.json", {"name": "Acme"})
    elsewhere = tmp_path / "inspect/acme.md"
    assert write_review_sheet(folder, elsewhere) == elsewhere
    assert elsewhere.read_text().startswith("# Review sheet: Acme")
    assert not (folder / "REVIEW-SHEET.md").exists()
    assert write_review_sheet(folder) == folder / "REVIEW-SHEET.md"
