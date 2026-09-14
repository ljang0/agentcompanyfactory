"""Offline proposals over the real compact CORT fixture; no models, VMs or npm."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from company_envs.storage import read, write
from company_envs.world.amplify import (
    Amplification,
    _get,
    _identity_check,
    amplify,
    apply_record_diff,
)
from company_envs.world.world_check import check_world

ROOT = Path(__file__).resolve().parents[1]
CORT = ROOT / "companies/cort"


@pytest.fixture
def company(tmp_path):
    folder = tmp_path / "companies/cort"
    folder.mkdir(parents=True)
    for name in ("company.json", "apps.json", "review.json"):
        shutil.copyfile(CORT / name, folder / name)
    shutil.copytree(CORT / "tasks/cort_o3", folder / "tasks/cort_o3")
    shutil.copytree(CORT / "world.v1-compact", folder / "world", ignore=shutil.ignore_patterns("calls"))
    return folder


def edit(path, before, after):
    return {"path": path, "before_json": json.dumps(before), "after_json": json.dumps(after)}


def proposal(folder):
    """Two fixed, physically distinct replacement-sofa widths within the access limit."""
    workflow = read(folder / "tasks/cort_o3/workflow.json")
    world = read(folder / "world/world.json")
    sheets = read(folder / "world/google_sheets_mock.state.json")
    zendesk = read(folder / "world/Zendesk_mock.state.json")
    variants = []
    for width in (210, 205):
        records = [
            {
                "target": "world",
                "record_path": "/assets/3",
                "edits": [
                    edit("/width_cm", world["assets"][3]["width_cm"], width),
                ],
            }
        ]
        # The historical room survey also fits a 215 cm sofa; that remains true
        # for both narrower replacements and is not a change to this stock item.
        for sheet_index, cell in ((0, "B5"),):
            values = sheets["sheets"][sheet_index]["data"][cell]
            records.append(
                {
                    "target": "google_sheets_mock",
                    "record_path": f"/sheets/{sheet_index}/data/{cell}",
                    "edits": [
                        edit(f"/{key}", values[key], values[key].replace("215 cm", f"{width} cm"))
                        for key in ("value", "formula")
                    ],
                }
            )
        comment = zendesk["comments"]["1311"][1]
        records.append(
            {
                "target": "Zendesk_mock",
                "record_path": "/comments/1311/1",
                "edits": [
                    edit(f"/{key}", comment[key], comment[key].replace("215 cm", f"{width} cm"))
                    for key in ("body", "html_body")
                ],
            }
        )
        material = workflow["initial_materials"][5]
        constraint = workflow["completion"]["feasible_path"]["constraint_checks"][0]
        variants.append(
            {
                "brief": f"Resolve H310's departure change. Assess the {width} cm replacement sofa against "
                "future demand and customer access, compare the supported visit structures, and "
                "reconcile actual pickups and the final billing handoff through September 28.",
                "records": records,
                "workflow_edits": [
                    edit(
                        "/initial_materials/5",
                        material,
                        material.replace("S2 is 215 cm", f"S2 is {width} cm"),
                    ),
                    edit(
                        "/completion/feasible_path/constraint_checks/0",
                        constraint,
                        constraint + f" S2's {width} cm width fits F311's 220 cm access limit.",
                    ),
                ],
                "reference_edits": [],
                "grader_edits": [],
            }
        )
    return {"variants": variants}


class FakeModels:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def call(self, job, prompt, response_type):
        self.calls.append((job, prompt, response_type))
        assert job == "task_amplify"
        assert response_type is Amplification
        return response_type.model_validate(deepcopy(self.result)), {"model": "fake", "call": 1}


def snapshot(folder):
    return {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def assert_no_variants(folder):
    assert not list((folder / "tasks").glob("*--v*"))
    assert not list((folder / "world/variants").glob("*"))


def test_identity_guard_protects_enriched_nested_user_copy():
    user = {"id": 1, "name": "Ann", "email": "ann@example.test"}
    base = {"world": {}, "a": {"tickets": [{"id": 7, "assignee": {**user, "extra": "metadata"}}]}}
    changed = deepcopy(base)
    changed["a"]["tickets"][0]["assignee"]["name"] = "Someone else"
    with pytest.raises(ValueError, match="identities changed"):
        _identity_check(base, changed, [{"app_id": "a"}], {"w1": {"a": user}})


def test_identity_guard_does_not_freeze_unrelated_ticket_with_same_id():
    user = {"id": 1, "name": "Ann"}
    base = {"world": {}, "a": {"tickets": [{"id": 1, "status": "open"}], "users": [user]}}
    changed = deepcopy(base)
    changed["a"]["tickets"][0]["status"] = "closed"
    _identity_check(base, changed, [{"app_id": "a"}], {"w1": {"a": user}})


def test_two_variants_replay_base_untouched_and_deterministic(company, tmp_path):
    before = snapshot(company)
    model = FakeModels(proposal(company))
    result = amplify(ROOT, company, "cort_o3", 2, models=model, seed=19)
    assert result["variants"] == ["cort_o3--v1", "cort_o3--v2"]
    assert len(model.calls) == 1
    assert '"seed": 19' in model.calls[0][1]
    assert '"decisive_records"' in model.calls[0][1]
    # The editable surface carries its current values; the whole world does not fit the prompt.
    assert '"base_states"' not in model.calls[0][1]
    payload = json.loads(model.calls[0][1][model.calls[0][1].index('\n{"count"') + 1 :])
    assert payload["allowed_records"] and all(
        row["editable_leaves"] and all(isinstance(p, str) for p in row["editable_leaves"])
        for row in payload["allowed_records"]
    )
    base_states = {a["app_id"]: read(company / a["state_file"]) for a in read(company / "apps.json")["apps"]}
    assert all(
        _get(base_states[row["target"]], row["record_path"] + path) == value
        for row in payload["allowed_records"]
        if row["target"] != "world"
        for path, value in row["editable_leaves"].items()
    )
    assert all((company / p).read_bytes() == content for p, content in before.items())
    workflow = read(company / "tasks/cort_o3/workflow.json")
    base = {p.name.removesuffix(".state.json"): read(p) for p in (company / "world").glob("*.state.json")}
    base["world"] = read(company / "world/world.json")
    for index, width in enumerate((210, 205), 1):
        task = company / f"tasks/cort_o3--v{index}"
        overlay = company / f"world/variants/cort_o3--v{index}"
        updated = read(task / "workflow.json")
        assert updated["variant_of"] == "cort_o3"
        assert str(width) in read(task / "assignment.json")["brief"]
        assert str(width) in updated["initial_materials"][5]
        assert read(task / "reference_expectations.json")["completion"] == updated["completion"]
        assert updated["worker_ids"] == workflow["worker_ids"]
        assert updated["success_criteria"] == workflow["success_criteria"]
        assert read(task / "amplification.json")["requires_review_and_calibration"]
        replayed = apply_record_diff(
            base, read(overlay / "diff.json")["records"], workflow["initial_materials"]
        )
        assert replayed["world"] == read(overlay / "world.json")
        assert replayed["world"]["assets"][3]["width_cm"] == width
        for app in base.keys() - {"world"}:
            assert read(overlay / f"{app}.state.json") == replayed[app]
            assert set(replayed[app]) == set(base[app])
        identities = read(overlay / "identities.json")
        assert identities == read(company / "world/identities.json")
        assert check_world(
            replayed["world"],
            {a: s for a, s in replayed.items() if a != "world"},
            identities,
            read(company / "company.json")["workers"],
            read(overlay / "SEED.json")["reference_date"],
        )["ok"]
    # Identical proposal + seed on a fresh baseline produces identical artifacts.
    other = tmp_path / "replay"
    for path, data in before.items():
        (other / path).parent.mkdir(parents=True, exist_ok=True)
        (other / path).write_bytes(data)
    amplify(ROOT, other, "cort_o3", 2, models=FakeModels(proposal(other)), seed=19)
    assert snapshot(other) == snapshot(company)


def test_undeclared_record_rejected_before_any_variant_published(company):
    proposed = proposal(company)
    proposed["variants"][1]["records"].append(
        {
            "target": "world",
            "record_path": "/locations/1",
            "edits": [edit("/name", "Raleigh Rental Showroom", "Changed showroom")],
        }
    )
    before = snapshot(company)
    with pytest.raises(ValueError, match="undeclared record"):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert snapshot(company) == before
    assert_no_variants(company)


def test_duplicates_ignore_brief_and_edit_order(company):
    proposed = proposal(company)
    proposed["variants"][1] = deepcopy(proposed["variants"][0])
    proposed["variants"][1]["brief"] += " Use the updated survey."
    proposed["variants"][1]["records"].reverse()
    with pytest.raises(ValueError, match="duplicate decisive"):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert_no_variants(company)


@pytest.mark.parametrize(
    "invalid", ["identity", "container", "stale_before", "reference_id", "wrong_count", "world_conflict"]
)
def test_invalid_proposal_rejected(company, invalid):
    proposed = proposal(company)
    variant = proposed["variants"][1]
    if invalid == "identity":
        variant["records"].append(
            {
                "target": "world",
                "record_path": "/workers/1",
                "edits": [
                    edit("/name", "Eli Brooks", "Someone Else"),
                ],
            }
        )
    elif invalid == "container":
        variant["records"].append(
            {
                "target": "world",
                "record_path": "/leases/0",
                "edits": [
                    edit("/monthly_rates", {"S1": 60, "B1": 90, "D1": 30}, {"S1": 90}),
                ],
            }
        )
    elif invalid == "stale_before":
        variant["records"][0]["edits"][0]["before_json"] = "999"
    elif invalid == "reference_id":
        variant["records"][0]["edits"] = [edit("/id", "S2", "S999")]
    elif invalid == "wrong_count":
        proposed["variants"].pop()
    elif invalid == "world_conflict":
        # A duplicate representation is coherent initially, then disagrees after
        # only the canonical width changes. Use the actual world_check function.
        path = company / "world/Zendesk_mock.state.json"
        state = read(path)
        state["tickets"][0]["assets"] = [{"id": "S2", "width_cm": 215}]
        write(path, state)
    messages = {
        "identity": "undeclared record|identities changed",
        "container": "container",
        "stale_before": "before value",
        "reference_id": "immutable",
        "wrong_count": "count",
        "world_conflict": "failed world_check",
    }
    with pytest.raises(ValueError, match=messages[invalid]):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert_no_variants(company)


def test_foreign_sheet_row_cannot_hide_under_allowed_parent(company):
    proposed = proposal(company)
    variant = proposed["variants"][0]
    variant["records"].append(
        {
            "target": "google_sheets_mock",
            "record_path": "/sheets/0",
            "edits": [edit("/data/B5/value", "SOFA-215; width 215 cm; 3.5 m3", "bad")],
        }
    )
    with pytest.raises(ValueError, match="undeclared record|escapes record"):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert_no_variants(company)


def test_shared_assignee_does_not_authorize_unrelated_ticket(company):
    path = company / "world/Zendesk_mock.state.json"
    state = read(path)
    index = len(state["tickets"])
    state["tickets"].append(
        {
            "id": 9999,
            "subject": "Routine furniture care",
            "assignee_id": 2,
            "status": "open",
        }
    )
    write(path, state)
    proposed = proposal(company)
    proposed["variants"][1]["records"].append(
        {
            "target": "Zendesk_mock",
            "record_path": f"/tickets/{index}",
            "edits": [edit("/status", "open", "solved")],
        }
    )
    with pytest.raises(ValueError, match="undeclared record"):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert_no_variants(company)


def test_reference_rebased_and_grader_expectations_updated(company):
    task = company / "tasks/cort_o3"
    initial = {p.name.removesuffix(".state.json"): read(p) for p in (company / "world").glob("*.state.json")}
    final = deepcopy(initial)
    final["Zendesk_mock"]["tickets"][0]["description"] = "Accepted sofa width 215 cm"
    write(task / "reference.json", {"final_states": final})
    write(
        task / "grader.json",
        {
            "grader": {
                "checks": [
                    {
                        "id": "width",
                        "criterion_ref": "/success_criteria/1",
                        "kind": "state",
                        "description": "Accepted sofa width is recorded",
                        "predicate": {
                            "app_id": "Zendesk_mock",
                            "selector": "$.tickets[?(@.id==1310)].description",
                            "operator": "equals",
                            "value_json": json.dumps("Accepted sofa width 215 cm"),
                        },
                    }
                ]
            },
            "initial_hash": "base-only",
            "calibration": "must not propagate",
        },
    )
    proposed = proposal(company)
    for variant, width in zip(proposed["variants"], (210, 205), strict=True):
        variant["reference_edits"] = [
            edit(
                "/final_states/Zendesk_mock/tickets/0/description",
                "Accepted sofa width 215 cm",
                f"Accepted sofa width {width} cm",
            )
        ]
        variant["grader_edits"] = [
            edit(
                "/checks/0/predicate/value_json",
                json.dumps("Accepted sofa width 215 cm"),
                json.dumps(f"Accepted sofa width {width} cm"),
            )
        ]
    amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    for index, width in enumerate((210, 205), 1):
        variant = company / f"tasks/cort_o3--v{index}"
        reference = read(variant / "reference.json")["final_states"]
        assert reference["Zendesk_mock"]["tickets"][0]["description"] == f"Accepted sofa width {width} cm"
        assert f"width {width} cm" in reference["google_sheets_mock"]["sheets"][0]["data"]["B5"]["value"]
        grader = read(variant / "grader.template.json")
        assert str(width) in grader["checks"][0]["predicate"]["value_json"]
        assert not (variant / "grader.json").exists()


def test_existing_output_never_overwritten_and_no_model_called(company):
    occupied = company / "tasks/cort_o3--v2"
    occupied.mkdir()
    (occupied / "sentinel").write_text("owned elsewhere")
    model = FakeModels(proposal(company))
    with pytest.raises(FileExistsError):
        amplify(ROOT, company, "cort_o3", 2, models=model)
    assert model.calls == []
    assert (occupied / "sentinel").read_text() == "owned elsewhere"


def test_undeclared_reference_record_rejected(company):
    state = read(company / "world/Zendesk_mock.state.json")
    write(company / "tasks/cort_o3/reference.json", {"diff": {"Zendesk_mock": {"views": state["views"]}}})
    proposed = proposal(company)
    proposed["variants"][1]["reference_edits"] = [
        edit("/diff/Zendesk_mock/views/0/title", state["views"][0]["title"], "Unrelated view"),
    ]
    with pytest.raises(ValueError, match="undeclared record"):
        amplify(ROOT, company, "cort_o3", 2, models=FakeModels(proposed))
    assert_no_variants(company)


@pytest.mark.parametrize("count,task_id", [(0, "cort_o3"), (True, "cort_o3"), (2, "../cort_o3")])
def test_invalid_arguments(company, count, task_id):
    with pytest.raises(ValueError):
        amplify(ROOT, company, task_id, count, models=FakeModels(proposal(company)))


def test_unaccepted_task_rejected_without_call(company):
    write(company / "review.json", {"review": {"tasks": [{"workflow_id": "cort_o3", "verdict": "revise"}]}})
    model = FakeModels(proposal(company))
    with pytest.raises(ValueError, match="accepted review"):
        amplify(ROOT, company, "cort_o3", 2, models=model)
    assert model.calls == []
