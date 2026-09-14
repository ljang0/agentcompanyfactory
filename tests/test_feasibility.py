import copy
import json

import pytest
from pydantic import BaseModel, Field, ValidationError
from test_pipeline import Critic, setup_job

from company_envs import pipeline, workflows
from company_envs.models import ModelOutputInvalid, Models, ModelUnavailable, strict_schema
from company_envs.report import build_report, export
from company_envs.schemas import NumericCheck, Workflow, WorkflowBatch
from company_envs.storage import digest, read, write


@pytest.fixture
def v2(workflow):
    raw = workflow.model_dump()
    raw["schema_version"] = "2"
    for phase in raw["phases"]:
        phase["when"] = "always"
    raw["completion"] = {
        "outcomes": [
            {
                "id": "released",
                "when": "Qualification supports release",
                "required_phase_ids": ["design", "qualify"],
                "observable": "Release disposition recorded",
            },
            {
                "id": "withdrawn",
                "when": "Customer withdraws before qualification",
                "required_phase_ids": ["design"],
                "observable": "Withdrawal and unfinished obligations recorded",
            },
        ],
        "feasible_path": {
            "outcome_id": "released",
            "steps": [
                {
                    "phase_id": "design",
                    "action_and_result": "PRIVATE: choose a viable instrument architecture",
                },
                {
                    "phase_id": "qualify",
                    "action_and_result": "PRIVATE: qualify the resulting process and release",
                },
            ],
            "constraint_checks": ["PRIVATE: requirements and process qualification are consistent"],
            "numeric_checks": [],
        },
    }
    raw["phases"][1]["when"] = "Customer has not withdrawn and design is ready"
    return Workflow.model_validate(raw)


def test_v1_roundtrip_does_not_change_historical_hashes(workflow):
    raw = workflow.model_dump()
    assert "completion" not in raw
    assert all("when" not in phase for phase in raw["phases"])
    assert digest(Workflow.model_validate(raw).model_dump()) == digest(raw)
    assert WorkflowBatch(workflows=[workflow]).model_dump()["workflows"][0] == raw


@pytest.mark.parametrize(
    "change,match",
    [
        ("missing_completion", "V2 requires"),
        ("empty_condition", "V2 requires"),
        ("wrong_version", "require workflow schema_version 2"),
        ("unknown_outcome", "unknown ids"),
        ("unknown_phase", "unknown ids"),
        ("duplicate_step", "repeats an id"),
        ("duplicate_outcome", "duplicate outcome"),
        ("missing_prerequisite", "hard prerequisite"),
        ("wrong_order", "hard prerequisite"),
        ("missing_milestone", "required phase"),
    ],
)
def test_invalid_completion_contracts_fail(v2, change, match):
    raw = v2.model_dump()
    c = raw["completion"]
    witness = c["feasible_path"]
    if change == "missing_completion":
        raw.pop("completion")
    elif change == "empty_condition":
        raw["phases"][0]["when"] = " "
    elif change == "wrong_version":
        raw["schema_version"] = "1"
    elif change == "unknown_outcome":
        witness["outcome_id"] = "unknown"
    elif change == "unknown_phase":
        c["outcomes"][0]["required_phase_ids"] = ["unknown"]
    elif change == "duplicate_step":
        witness["steps"].append(witness["steps"][0])
    elif change == "duplicate_outcome":
        c["outcomes"].append(c["outcomes"][0])
    elif change == "missing_prerequisite":
        witness["steps"] = witness["steps"][1:]
    elif change == "wrong_order":
        witness["steps"].reverse()
    elif change == "missing_milestone":
        witness["steps"] = witness["steps"][:1]
    with pytest.raises(ValidationError, match=match):
        Workflow.model_validate(raw)


def test_alternative_ending_can_skip_inapplicable_phases(v2):
    raw = v2.model_dump()
    raw["completion"]["feasible_path"]["outcome_id"] = "withdrawn"
    raw["completion"]["feasible_path"]["steps"] = raw["completion"]["feasible_path"]["steps"][:1]
    assert Workflow.model_validate(raw).completion.feasible_path.outcome_id == "withdrawn"


@pytest.mark.parametrize(
    "left,relation,right,valid",
    [
        ("600*4/60", "<=", "18", False),
        ("600*4/60", "<=", "48", True),
        ("10", ">=", "11", False),
        ("0.1+0.2", "==", "0.3", True),
        ("1000000000000000", "==", "999999999999999", False),
        ("__import__('os')", "==", "0", False),
        ("1/0", "<=", "1", False),
    ],
)
def test_numeric_feasibility_checks(left, relation, right, valid):
    values = {
        "constraint": "Assembly capacity",
        "left": left,
        "relation": relation,
        "right": right,
        "unit": "hours",
    }
    if valid:
        assert NumericCheck(**values).unit == "hours"
    else:
        with pytest.raises(ValidationError):
            NumericCheck(**values)


def test_worker_assignment_excludes_witness_and_grading(v2):
    packet = v2.assignment()
    assert set(packet) == {"workflow_id", "company_id", "title", "brief"}
    assert "PRIVATE" not in json.dumps(packet)
    assert "completion" not in packet and "success_criteria" not in packet


@pytest.mark.parametrize("batch_type", [WorkflowBatch, workflows.DesignBatch])
def test_generation_schema_is_version_pinned(batch_type):
    old = workflows.generation_schema(batch_type, "1")
    new = workflows.generation_schema(batch_type, "2")
    assert old["$defs"]["Workflow"]["properties"]["schema_version"]["const"] == "1"
    assert "completion" not in old["$defs"]["Workflow"]["properties"]
    assert "CompletionContract" not in old["$defs"]
    assert new["$defs"]["Workflow"]["properties"]["completion"] == {"$ref": "#/$defs/CompletionContract"}
    assert new["$defs"]["Workflow"]["properties"]["schema_version"]["const"] == "2"
    assert "completion" in new["$defs"]["Workflow"]["required"]
    assert "when" in new["$defs"]["Phase"]["required"]


def test_new_run_freezes_v2(root):
    directory = pipeline.new_run(root, 1, 1)
    assert read(directory / "run.json")["config"]["design"]["workflow_version"] == "2"


def test_expand_rejects_silent_downgrade(company, workflow):
    class Author:
        def call(self, *args, **kwargs):
            return WorkflowBatch(workflows=[workflow]), {"model": "codex/gpt-test"}

    with pytest.raises(ModelOutputInvalid, match="requires workflow schema_version 2"):
        workflows.expand(Author(), "skill", company, [], ["release"], workflow_version="2")


@pytest.mark.parametrize("agency", [False, True])
def test_v2_prepare_publish_export_and_tamper_detection(root, company, v2, monkeypatch, agency):
    directory, state, path = setup_job(root, company, v2)
    state["config"]["design"]["workflow_version"] = "2"
    state["config"]["design"]["agency"] = agency
    (path / "workflows.json").unlink()

    def expand(*args, **kwargs):
        assert kwargs["workflow_version"] == "2"
        return [v2], {"model": "codex/author", "job": "expand"}

    def design(*args, **kwargs):
        generated, receipt = expand(*args, **kwargs)
        return company, generated, receipt, {"amendment": None}

    monkeypatch.setattr(pipeline.workflow_job, "expand", expand)
    monkeypatch.setattr(pipeline.workflow_job, "design", design)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    assert export(root, directory, state)["counts"]["workflows"] == 1
    assert read(directory / "assignments.json")[v2.id] == v2.assignment()
    assert read(directory / "schemas/workflow.schema.json")["properties"]["schema_version"]["const"] == "2"
    published = read(root / state["entries"][0]["workflow_path"])
    assert published["completion"] == v2.model_dump()["completion"]
    original_review = read(path / "review.json")
    changed = copy.deepcopy(published)
    changed["completion"]["feasible_path"]["constraint_checks"] = ["Changed after review"]
    write(path / "workflows.json", [changed])
    assert build_report(root, directory, state)["validation_errors"]
    assert read(path / "review.json") == original_review


def test_v2_publish_cannot_bypass_contract(root, company, workflow, monkeypatch):
    directory, state, _ = setup_job(root, company, workflow)
    state["config"]["design"]["workflow_version"] = "2"
    with pytest.raises(ValueError, match="requires workflow schema_version 2"):
        pipeline.publish(root, directory, state, company.id, None)


def test_v2_prepare_rejects_legacy_cached_draft(root, company, workflow):
    directory, state, _ = setup_job(root, company, workflow)
    state["config"]["design"]["workflow_version"] = "2"
    with pytest.raises(ValueError, match="requires workflow schema_version 2"):
        pipeline.prepare(root, directory, state, state["jobs"][company.id])


def test_provider_schema_removes_descriptive_ref_siblings():
    class Child(BaseModel):
        value: str

    class Parent(BaseModel):
        child: Child = Field(description="A useful description", title="Child field")

    schema = strict_schema(Parent)
    assert schema["properties"]["child"] == {"$ref": "#/$defs/Child"}
    assert schema["required"] == ["child"]


def test_provider_error_on_stdout_is_not_lost(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        (directory / "stderr.txt").write_text("")
        (directory / "stdout.jsonl").write_text(
            json.dumps(
                {"type": "turn.failed", "error": {"message": "invalid_json_schema: annotated reference"}}
            )
        )
        return 1, 0.1

    monkeypatch.setattr("company_envs.models.execute", execute)
    config = {
        "models": {"expand": ["codex/gpt-test"], "reasoning": "high"},
        "generation": {"completion_timeout_seconds": 1},
    }
    with pytest.raises(ModelUnavailable, match="invalid_json_schema"):
        Models(config, tmp_path).call("expand", "test", WorkflowBatch)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert "annotated reference" in receipt["error"]
