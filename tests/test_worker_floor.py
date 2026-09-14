from types import SimpleNamespace

import pytest
from test_pipeline import setup_job

from company_envs import pipeline, workflows
from company_envs.audit import audit_run
from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid
from company_envs.report import export
from company_envs.research import research
from company_envs.schemas import Company, Workflow, WorkflowBatch, minimum_workers, validate_workflow
from company_envs.storage import read


def three_workers(company, workflow):
    co, task = company.model_dump(), workflow.model_dump()
    co["workers"].append(
        {**co["workers"][0], "id": "quality", "responsibility": "Assess measurement validity"}
    )
    co["teams"][0]["worker_ids"].append("quality")
    co["outlines"][0]["worker_ids"].append("quality")
    task["worker_ids"].append("quality")
    task["phases"][1]["worker_ids"].append("quality")
    task["contributions"].append(
        {**task["contributions"][0], "worker_id": "quality", "work": "Assess measurement validity"}
    )
    return Company.model_validate(co), Workflow.model_validate(task)


def test_three_worker_floor_and_legacy_compatibility(company, workflow):
    assert minimum_workers({}) == 2
    validate_workflow(company, workflow)
    with pytest.raises(ValueError, match="at least 3"):
        validate_workflow(company, workflow, minimum_workers=3)
    co, task = three_workers(company, workflow)
    validate_workflow(co, task, minimum_workers=3)
    # Three workers do not require three teams or different occupation codes.
    assert len(co.teams) == 2
    assert len({w.soc for w in co.workers}) == 1


@pytest.mark.parametrize("value", [1, 0, -1, True, 3.5, "3"])
def test_invalid_worker_policy(value):
    with pytest.raises(ValueError, match="minimum_workers"):
        minimum_workers({"design": {"minimum_workers": value}})


def test_new_run_and_export_freeze_three_worker_floor(root):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    assert minimum_workers(state["config"]) == 3
    export(root, directory, state)
    schema = read(directory / "schemas/workflow.schema.json")
    assert schema["properties"]["worker_ids"]["minItems"] == 3


@pytest.mark.parametrize("mode", ["design", "expand"])
@pytest.mark.parametrize("count", [2, 3])
def test_author_schema_and_runtime_enforce_same_floor(root, company, workflow, mode, count):
    if count == 3:
        company, workflow = three_workers(company, workflow)

    def call(job, prompt, response_type, **kwargs):
        assert kwargs["schema"]["$defs"]["Workflow"]["properties"]["worker_ids"]["minItems"] == 3
        raw = {"workflows": [workflow.model_dump()]}
        if mode == "design":
            raw.update(amendment=None, selection_reason="Complementary technical work")
        return response_type.model_validate(raw), {"call_id": "test"}

    model = SimpleNamespace(config={"design": {"minimum_workers": 3}}, call=call)

    def run():
        if mode == "design":
            return workflows.design(
                model,
                "skill",
                company,
                [],
                Catalogs(root / "catalogs"),
                [],
                1,
                selected=["release"],
                read_only_tools=False,
                allow_amendment=False,
            )
        return workflows.expand(model, "skill", company, [], ["release"])

    if count == 2:
        with pytest.raises(ModelOutputInvalid, match="at least 3"):
            run()
    else:
        run()


def test_research_exposes_and_checks_roster_and_outline_floor(root, company):
    def call(job, prompt, response_type, **kwargs):
        schema = kwargs["schema"]
        assert schema["properties"]["workers"]["minItems"] == 3
        assert schema["$defs"]["Outline"]["properties"]["worker_ids"]["minItems"] == 3
        return company, {"call_id": "test"}

    model = SimpleNamespace(config={"design": {"minimum_workers": 3}}, call=call)
    with pytest.raises(ModelOutputInvalid, match="at least 3"):
        research(model, Catalogs(root / "catalogs"), "skill", company, {})


def test_publish_and_audit_cannot_bypass_run_floor(root, company, workflow):
    directory, state, _path = setup_job(root, company, workflow)
    state["config"]["design"]["minimum_workers"] = 3
    with pytest.raises(ValueError, match="at least 3"):
        pipeline.publish(root, directory, state, company.id, None)
    audit = audit_run(root, directory, state)
    assert "at least 3" in audit["stages"]["expansion"]["errors"][company.id]


@pytest.mark.parametrize("batch", [WorkflowBatch, workflows.DesignBatch])
def test_legacy_output_schema_keeps_two_workers(batch):
    assert (
        workflows.generation_schema(batch, "2")["$defs"]["Workflow"]["properties"]["worker_ids"]["minItems"]
        == 2
    )
