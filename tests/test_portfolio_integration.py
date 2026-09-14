import json
from types import SimpleNamespace
from typing import ClassVar

import numpy as np
import pytest
from test_pipeline import setup_job

from company_envs import pipeline
from company_envs.catalogs import Catalogs
from company_envs.portfolio import load_history
from company_envs.schemas import Candidate, Discovery, Review
from company_envs.storage import digest, read, write


class RecordingCritic:
    payloads: ClassVar[list] = []

    def __init__(self, config, directory):
        pass

    def call(self, job, prompt, response_type, avoid=(), **kwargs):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        return Review(
            company_verdict="accept",
            company_reasons=[],
            tasks=[
                {
                    "workflow_id": workflow["id"],
                    "verdict": "accept",
                    "quality": 4,
                    "novelty": "distinct",
                    "duplicate_of": "",
                    "reasons": [],
                }
                for workflow in payload["workflows"]
            ],
        ), {"model": "codex/test-critic", "status": "complete", "prompt_hash": digest(prompt)}


def test_ordinary_discovery_rejects_aliases_inside_new_batch(root, monkeypatch):
    directory = pipeline.new_run(root, 4, 8)
    state = read(directory / "run.json")
    assert "excluded_companies" not in state
    monkeypatch.setattr(pipeline, "sector_targets", lambda sectors, target: {"Manufacturing": 4})

    def discover(models, catalogs, prompt, sector, existing, coverage, count):
        identities = [
            ("original", "Example and Sons Incorporated", "https://example.test"),
            ("same-host", "A distinct display name", "https://www.example.test/about"),
            ("legal-alias", "Example & Sons, Inc.", "https://alias.test"),
            ("different", "Other Instruments", "https://different.test"),
        ]
        return Discovery(
            candidates=[
                Candidate(id=cid, real_firm=name, website=url, sector=sector, reason="Operating evidence")
                for cid, name, url in identities
            ]
        ), {"call_id": "discovery-test"}

    monkeypatch.setattr(pipeline.research_job, "discover", discover)
    assert pipeline.fill_queue(root, directory, state, Catalogs(directory / "catalogs"))
    assert set(state["jobs"]) == {"original", "different"}
    assert {row["candidate"]["id"] for row in state["excluded_discoveries"]} == {
        "same-host",
        "legal-alias",
    }


@pytest.mark.parametrize("include_history", [True, False])
def test_publish_uses_frozen_history_current_entries_and_batch(
    root, company, workflow, monkeypatch, include_history
):
    RecordingCritic.payloads = []
    monkeypatch.setattr(pipeline, "Models", RecordingCritic)
    encoder = SimpleNamespace(encode=lambda text: np.array([1.0, 0.0]))
    old_directory, old_state, _ = setup_job(root, company, workflow)
    pipeline.publish(root, old_directory, old_state, company.id, encoder)
    write(old_directory / "run.json", old_state)
    directory, state, path = setup_job(root, company, workflow)
    history = load_history(directory, state)
    assert len(history) == 1  # Real prior publication, not a mocked history loader.
    if not include_history:
        state.pop("portfolio")  # Historical configurations have no frozen portfolio.

    second = workflow.model_copy(update={"id": "example_second", "outline_id": "second"})
    company.outlines.append(company.outlines[0].model_copy(update={"id": "second"}))
    write(path / "company.json", company.model_dump())
    write(path / "workflows.json", [workflow.model_dump(), second.model_dump()])
    current = workflow.model_copy(update={"id": "already_accepted"})
    current_path = "data/workflows/already-accepted.json"
    write(root / current_path, current.model_dump())
    state["entries"].append(
        {
            "workflow_id": current.id,
            "verdict": "accept",
            "workflow_path": current_path,
            "family_id": current.id,
        }
    )
    # Resume must not consult new files in the previous run.
    write(old_directory / "run.json", {"changed_after_snapshot": True})
    pipeline.publish(root, directory, state, company.id, encoder)
    neighbors = RecordingCritic.payloads[-1]["nearby_workflows"]
    for design, other in ((workflow, second), (second, workflow)):
        expected = {current.id, other.id}
        if include_history:
            expected.add(history[0].id)
        assert {row["workflow_id"] for row in neighbors[design.id]} == expected
        assert all(row["workflow"]["id"] == row["workflow_id"] for row in neighbors[design.id])


def test_coverage_contains_bounded_substantive_history_and_preserves_legacy(root, workflow):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    workflows = [workflow.model_copy(update={"id": f"previous_{index}"}) for index in range(30)]
    snapshot = {
        "schema_version": "1",
        "workflows": [
            {"workflow": w.model_dump(), "content_hash": digest(w.model_dump())} for w in workflows
        ],
    }
    write(directory / "portfolio.json", snapshot)
    state["portfolio"] = {"path": "portfolio.json", "hash": digest(snapshot), "count": 30}
    catalogs = Catalogs(directory / "catalogs")
    result = pipeline.coverage(root, state, catalogs)
    assert result["companies"] == result["tasks"] == 0
    assert result["portfolio"]["total_workflows"] == 30
    assert result["portfolio"]["shown_workflows"] == 24
    row = result["portfolio"]["work"][0]
    assert row["decision_problem"] == workflow.decision_problem
    assert row["contributions"] == [contribution.work for contribution in workflow.contributions]
    assert row["deliverables"] == workflow.deliverables
    del state["portfolio"]
    legacy = pipeline.coverage(root, state, catalogs)
    assert "portfolio" not in legacy
    assert legacy == {key: value for key, value in result.items() if key != "portfolio"}
