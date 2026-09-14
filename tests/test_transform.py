"""Offline generalization against the checked-in compact CORT seed."""

import shutil
from copy import deepcopy
from datetime import date
from itertools import pairwise
from pathlib import Path

import pytest

from company_envs.storage import digest, read, write
from company_envs.world import grader
from company_envs.world.hub_world import load_world
from company_envs.world.transform import transform_company, verify_transform

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def company(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    for name in ("apps.json", "company.json", "MANIFEST.json"):
        shutil.copyfile(ROOT / "companies/cort" / name, folder / name)
    shutil.copytree(
        ROOT / "companies/cort/world.v1-compact", folder / "world", ignore=shutil.ignore_patterns("calls")
    )
    task = folder / "tasks/cort_o3"
    shutil.copytree(ROOT / "companies/cort/tasks/cort_o3", task)
    initial = {row["app_id"]: read(folder / row["state_file"]) for row in read(folder / "apps.json")["apps"]}
    checks = [
        {
            "id": "ticket_status",
            "criterion_ref": "/success_criteria/0",
            "kind": "state",
            "description": "The targeted ticket was solved",
            "predicate": {
                "app_id": "Zendesk_mock",
                "selector": "$.tickets[?(@.id==1310)].status",
                "operator": "equals",
                "value_json": '"solved"',
            },
            "guards": [
                {"app_id": "Zendesk_mock", "selector": '$.comments["1310"]', "operator": "exists"},
                {
                    "app_id": "Zendesk_mock",
                    "selector": "$.tickets[?(@.id==1310)].id",
                    "operator": "equals",
                    "value_json": "1310",
                },
                {
                    "app_id": "Zendesk_mock",
                    "selector": "$.tickets",
                    "operator": "count_gte",
                    "value_json": "1",
                },
            ],
        },
        {
            "id": "sheet_evidence",
            "criterion_ref": "/success_criteria/1",
            "kind": "artifact",
            "description": "A substantive allocation analysis",
            "rubric": "Evaluate the recorded allocation analysis against source evidence.",
            "app_paths": [
                {"app_id": "google_sheets_mock", "selector": '$.sheets[?(@.id=="assets")].data["B1"]'}
            ],
            "material_paths": ["w1/exports/L310-ledger.csv"],
        },
    ]
    workflow = read(task / "workflow.json")
    workflow["success_criteria"] = [{"method": "state"}, {"method": "artifact"}]
    write(task / "workflow.json", workflow)
    draft = {
        "version": grader.VERSION,
        "grader": grader.TaskGrader.model_validate({"checks": checks}).model_dump(),
        "initial_hash": digest(initial),
        "criteria_hash": digest(workflow["success_criteria"]),
        "brief_hash": digest(read(task / "assignment.json")),
        "grading_context_hash": digest(grader._grading_context(folder, "cort_o3")),
        "initial_material_hashes": grader._material_hashes(folder),
    }
    write(task / "grader.json", draft)
    final = deepcopy(initial)
    next(t for t in final["Zendesk_mock"]["tickets"] if t["id"] == 1310)["status"] = "solved"
    write(task / "reference.json", {"final_states": final})
    write(task / "golden.json", {"final_states": final})
    return folder


class Client:
    def __init__(self, state):
        self.state = state

    def inspect(self, sid):
        return {"initial_state": self.state, "current_state": self.state, "state_diff": {}}


def test_cort_rename_shift_and_calibration(company, tmp_path):
    original_plan = load_world(company, ROOT)
    write(company / "runtime/sessions.json", {"sid": "original-double"})
    original = grader.calibrate(
        company, "cort_o3", {row["app_id"]: Client(row["state"]) for row in original_plan}
    )
    output = tmp_path / "renamed"
    manifest = transform_company(company, output, rename_seed=42, shift_days=30)
    report = verify_transform(company, output)
    assert report["ok"], report
    assert report["world_check"]["errors"] == 0
    assert len(report["grader_checks"]) == 6
    assert all(row["ok"] for row in report["grader_checks"])
    plan = load_world(output)
    ids = manifest["id_map"]
    assert isinstance(ids["1310"], int)
    assert read(output / "apps.json")["workers"][0] == ids["w1"]
    assert (output / f"world/materials/{ids['w1']}/exports/{ids['L310']}-ledger.csv").is_file()
    sheet = next(row for row in plan if row["app_id"] == "google_sheets_mock")["state"]
    assert "B1" in sheet["sheets"][0]["data"]  # A coordinate, not the asset ID.
    world = read(output / "world/world.json")
    assert world["district_totals"]["crews"] == 4
    assert world["snapshot_at"] == manifest["date_map"]["2026-09-08"] + "T12:00:00Z"
    task_id = ids["cort_o3"]
    write(output / "runtime/sessions.json", {"sid": "double"})
    clients = {row["app_id"]: Client(row["state"]) for row in plan}
    calibration = grader.calibrate(output, task_id, clients)
    assert calibration["initial_score"] == 0
    assert calibration["reference_score"] == 1
    assert calibration["accepted"]
    assert original["accepted"]
    assert original["initial_score"] == calibration["initial_score"]
    assert original["reference_score"] == calibration["reference_score"]
    second = tmp_path / "same"
    assert transform_company(company, second, rename_seed=42, shift_days=30) == manifest
    assert verify_transform(company, second)["ok"]


def test_incomplete_map_leaks_are_detected(company, tmp_path):
    output = tmp_path / "partial"
    transform_company(company, output, id_map={"H310": "household_replaced"}, shift_days=30)
    report = verify_transform(company, output)
    assert not report["ok"]
    assert any("source ID leak: incomplete map" in error for error in report["errors"])


def test_repeated_ids_dates_in_keys_and_paths_and_explicit_map(company, tmp_path):
    name = "world/materials/w1/timeline-2026-09-08.json"
    write(
        company / name,
        {"2026-09-08": "H310 H310 H310 at 2026-09-08T08:15:30.123456-04:00"},
    )
    first = tmp_path / "generated"
    manifest = transform_company(company, first, rename_seed=18, shift_days=30)
    second = tmp_path / "explicit"
    explicit = transform_company(company, second, id_map=manifest["id_map"], shift_days=30)
    assert explicit["output_hashes"] == manifest["output_hashes"]
    shifted = manifest["date_map"]["2026-09-08"]
    target = f"world/materials/{manifest['id_map']['w1']}/timeline-{shifted}.json"
    node = read(second / target)
    assert node == {shifted: f"{manifest['id_map']['H310']} " * 3 + f"at {shifted}T08:15:30.123456-04:00"}
    assert verify_transform(company, second)["ok"]


def test_company_without_grader(company, tmp_path):
    (company / "tasks/cort_o3/grader.json").unlink()
    output = tmp_path / "without_grader"
    transform_company(company, output, rename_seed=2, shift_days=30)
    report = verify_transform(company, output)
    assert report["ok"], report
    assert report["grader_checks"] == []


@pytest.mark.parametrize("shift", [30, -30, 0])
def test_calendar_adjustments_preserve_order_and_weekdays(company, tmp_path, shift):
    output = tmp_path / "shifted"
    manifest = transform_company(company, output, shift_days=shift)
    pairs = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in manifest["date_map"].items()]
    assert all(b < d for (_, b), (_, d) in pairwise(pairs))
    assert all(b.weekday() < 5 for a, b in pairs if a.weekday() < 5)
    if shift:
        assert any("weekday" in row["reasons"] for row in manifest["adjustments"])
        assert all(row["adjustment_days"] * shift > 0 for row in manifest["adjustments"])
    else:
        assert not manifest["adjustments"]
        assert all(a == b for a, b in pairs)
    assert verify_transform(company, output)["ok"]


def test_disable_business_calendar(company, tmp_path):
    output = tmp_path / "calendar_days"
    manifest = transform_company(company, output, shift_days=30, business_calendar=False)
    assert manifest["adjustments"] == []
    assert all(
        (date.fromisoformat(b) - date.fromisoformat(a)).days == 30 for a, b in manifest["date_map"].items()
    )
    assert verify_transform(company, output)["ok"]


@pytest.mark.parametrize("tamper", ["leak", "selector", "extra", "date", "map"])
def test_verify_detects_tampering_even_with_updated_hashes(company, tmp_path, tamper):
    output = tmp_path / "corrupted"
    manifest = transform_company(company, output, rename_seed=7, shift_days=30)
    name = "world/world.json"
    node = read(output / name)
    if tamper == "leak":
        node["workers"][0]["id"] = "w1"
    elif tamper == "date":
        node["snapshot_at"] = "2099-01-01T12:00:00Z"
    elif tamper == "extra":
        name, node = "tasks/extra.json", {"id": "H310"}
    elif tamper == "map":
        manifest["id_map"].pop("H310")
    else:
        name = f"tasks/{manifest['id_map']['cort_o3']}/grader.json"
        node = read(output / name)
        node["grader"]["checks"][0]["predicate"]["selector"] = "$.tickets[?(@.id==9999999)].status"
    write(output / name, node)
    manifest["output_hashes"][name] = digest((output / name).read_bytes())
    write(output / "TRANSFORM.json", manifest)
    report = verify_transform(company, output)
    assert not report["ok"]
    if tamper == "selector":
        assert "grader resolution changed" in report["errors"]


@pytest.mark.parametrize("mapping", [{"H310": "H311"}, {"H310": "same", "H311": "same"}, {"1": "text"}])
def test_bad_maps_refused_before_output(company, tmp_path, mapping):
    output = tmp_path / "bad"
    with pytest.raises(ValueError):
        transform_company(company, output, id_map=mapping)
    assert not output.exists()


def test_refuses_overwrite_and_nested_output(company, tmp_path):
    for output in [company, company / "nested", tmp_path]:
        with pytest.raises(ValueError, match="new folder outside"):
            transform_company(company, output, rename_seed=1)


def test_numeric_ids_in_native_references_and_material_paths_are_renamed(company, tmp_path):
    path = company / "world/materials/w1/tickets/1310.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Open Zendesk_mock.tickets#1310 or https://desk.example/tickets/1310.\nThere are 1310 chairs.\n"
    )
    output = tmp_path / "references"
    manifest = transform_company(company, output, rename_seed=5)
    ids = manifest["id_map"]
    target = output / f"world/materials/{ids['w1']}/tickets/{ids['1310']}.md"
    assert target.is_file()
    assert (
        target.read_text()
        == f"Open Zendesk_mock.tickets#{ids['1310']} or https://desk.example/tickets/{ids['1310']}.\nThere are 1310 chairs.\n"
    )
    assert verify_transform(company, output)["ok"]


def test_free_text_only_codes_with_lowercase_suffix_are_renamed(company, tmp_path):
    path = company / "world/materials/w1/case-note.md"
    path.write_text("Please refer to CASE-abc123 when you call. CASE-abc123 is the same case.\n")
    output = tmp_path / "free_text"
    manifest = transform_company(company, output, rename_seed=8)
    assert "CASE-abc123" in manifest["id_map"]
    text = (output / f"world/materials/{manifest['id_map']['w1']}/case-note.md").read_text()
    assert "CASE-abc123" not in text
    assert text.count(manifest["id_map"]["CASE-abc123"]) == 2
    assert verify_transform(company, output)["ok"]
