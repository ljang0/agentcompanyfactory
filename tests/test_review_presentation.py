import copy
import json

import pytest
from test_contracts import verdict
from test_pipeline import Critic, setup_job

from company_envs import pipeline
from company_envs.report import build_report
from company_envs.review import review, review_payload
from company_envs.schemas import Workflow
from company_envs.storage import bound_review, digest, read, write


@pytest.fixture
def example_v2(workflow):
    raw = workflow.model_dump()
    raw["schema_version"] = "2"
    for phase in raw["phases"]:
        phase["when"] = "always"
    raw["completion"] = {
        "outcomes": [
            {
                "id": "released",
                "when": "Qualification supports release",
                "required_phase_ids": [phase["id"] for phase in raw["phases"]],
                "observable": "Release disposition recorded",
            }
        ],
        "feasible_path": {
            "outcome_id": "released",
            "steps": [
                {"phase_id": phase["id"], "action_and_result": "PRIVATE example"} for phase in raw["phases"]
            ],
            "constraint_checks": ["PRIVATE consistency check"],
            "numeric_checks": [],
        },
    }
    return Workflow.model_validate(raw)


def test_split_presentation_preserves_full_binding_and_never_claims_blinding(company, example_v2):
    originals = [example_v2.model_dump()]
    before = copy.deepcopy(originals)
    legacy = review_payload(company.model_dump(), originals, [], [], {})
    assert list(legacy) == ["company", "workflows", "evidence_status", "source_excerpts", "nearby_workflows"]
    assert legacy["workflows"] == originals
    split = review_payload(company.model_dump(), originals, [], [], {}, separate_witness=True)
    assert originals == before
    assert "feasible_path" not in split["workflows"][0]["completion"]
    assert split["workflows"][0]["completion"]["outcomes"] == originals[0]["completion"]["outcomes"]
    assert split["author_witnesses"][example_v2.id] == originals[0]["completion"]["feasible_path"]
    assert split["review_presentation"]["blind"] is False

    class RecordingCritic:
        def call(self, stage, prompt, response_type, **options):
            assert json.loads(prompt.split("\n", 1)[1]) == split
            return verdict(example_v2), {"model": "codex/reviewer", "prompt_hash": digest(prompt)}

    result = review(
        RecordingCritic(),
        "skill",
        company,
        [example_v2],
        [],
        [],
        {},
        {"codex/author"},
        separate_witness=True,
    )
    assert result["input_hash"] == bound_review(
        company.model_dump(), originals, [], "skill", {"codex/author"}, "different_model", None
    )


def test_split_review_export_and_witness_tamper_detection(root, company, example_v2, monkeypatch):
    directory, state, path = setup_job(root, company, example_v2)
    state["config"]["design"]["workflow_version"] = "2"
    state["config"]["design"]["review_separate_witness"] = True
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    assert read(path / "review.json")["separate_witness"]
    assert not build_report(root, directory, state)["validation_errors"]
    state["config"]["design"]["review_separate_witness"] = False
    assert build_report(root, directory, state)["validation_errors"]
    state["config"]["design"]["review_separate_witness"] = True
    changed = read(path / "workflows.json")
    changed[0]["completion"]["feasible_path"]["constraint_checks"] = ["Unreviewed change"]
    write(path / "workflows.json", changed)
    assert build_report(root, directory, state)["validation_errors"]
