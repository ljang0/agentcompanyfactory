import pytest

from company_envs import pipeline
from company_envs.catalogs import Catalogs
from company_envs.schemas import Candidate, Discovery
from company_envs.storage import read, write


@pytest.mark.parametrize("limit,expected", [(None, 3), (1, 1), (2, 2), (10, 3)])
def test_initial_job_batch_budget_and_legacy_behavior(root, monkeypatch, limit, expected):
    directory = pipeline.new_run(root, 10, 20)
    state = read(directory / "run.json")
    state["config"]["generation"].pop("workflow_batch_size", None)
    if limit is not None:
        state["config"]["generation"]["workflow_batch_size"] = limit
    monkeypatch.setattr(pipeline, "coverage", lambda *args: {"companies": 0, "tasks": 0, "sectors": {}})

    def discover(models, catalogs, prompt, sector, existing, cov, count):
        return Discovery(
            candidates=[
                Candidate(
                    id="new-company",
                    real_firm="New Company",
                    sector=sector,
                    website="https://example.com",
                    reason="Test candidate",
                )
            ]
        ), {"call_id": "test"}

    monkeypatch.setattr(pipeline.research_job, "discover", discover)
    assert pipeline.fill_queue(root, directory, state, Catalogs(directory / "catalogs"))
    assert state["jobs"]["new-company"]["expansion_count"] == expected


def test_extra_outlines_are_bounded_without_being_lost(root, company, workflow, monkeypatch):
    directory = pipeline.new_run(root, 1, 5)
    state = read(directory / "run.json")
    extras = ["second", "third", "fourth", "fifth"]
    company.outlines.extend(company.outlines[0].model_copy(update={"id": name}) for name in extras)
    cp = "data/companies/example.json"
    write(root / cp, company.model_dump())
    state["companies"][company.id] = cp
    state["jobs"][company.id] = {
        "id": company.id,
        "candidate": {"id": company.id},
        "status": "reviewed",
    }
    write(directory / "jobs" / company.id / "workflows.json", [workflow.model_dump()])
    monkeypatch.setattr(pipeline, "coverage", lambda *args: {})
    seen = []
    for _ in range(2):
        assert pipeline.enqueue_extra(root, directory, state, None)
        job = next(j for j in state["jobs"].values() if j["status"] == "queued")
        assert job["expansion_count"] == 2
        seen.extend(job["outline_ids"])
        write(
            directory / "jobs" / job["id"] / "workflows.json",
            [
                workflow.model_copy(update={"id": "example_" + name, "outline_id": name}).model_dump()
                for name in job["outline_ids"]
            ],
        )
        job["status"] = "reviewed"
    assert seen == extras
    assert not pipeline.enqueue_extra(root, directory, state, None)


@pytest.mark.parametrize("value", ["0", "-1", "false", "1.5"])
def test_rejects_invalid_batch_budget_before_creating_run(root, value):
    config = root / "config.toml"
    config.write_text(config.read_text().replace("workflow_batch_size = 2", "workflow_batch_size = " + value))
    with pytest.raises(ValueError, match="workflow_batch_size must be a positive integer"):
        pipeline.new_run(root)
    assert not (root / "runs").exists()
