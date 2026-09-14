"""Brief repairs keep the task contract and original review evidence unchanged."""

import json
from copy import deepcopy

import pytest
from test_contracts import verdict
from test_pipeline import Critic, setup_job

from company_envs import pipeline
from company_envs.review import apply_brief_fixes, validate_review
from company_envs.schemas import Review
from company_envs.storage import read, write

FIX = "Develop the instrument and check that it meets the customer needs. Decide if it is ready to build."


def brief_verdict(workflow, **changes):
    return verdict(
        workflow,
        **{
            "verdict": "modify_brief",
            "severity": "P1",
            "brief_fix": FIX,
            "reasons": ["The brief does not name the instrument that the supplied requirements describe."],
            **changes,
        },
    )


@pytest.mark.parametrize(
    "severity,outcome",
    [("P0", "reject"), ("P1", "modify_brief"), ("P2", "accept"), ("P2", "modify_brief"), ("P3", "accept")],
)
def test_severity_rules(workflow, severity, outcome):
    result = brief_verdict(
        workflow, severity=severity, verdict=outcome, brief_fix=FIX if outcome == "modify_brief" else ""
    )
    validate_review(result, [workflow], {})
    assert Review.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"severity": "P0"}, "P0 does not allow"),
        ({"severity": "P3"}, "P3 does not allow"),
        ({"severity": None}, "needs P1 or P2"),
        ({"brief_fix": "  "}, "nonempty brief_fix"),
        ({"quality": 2}, "usable quality"),
        ({"reasons": []}, "actionable reasons"),
        ({"verdict": "accept"}, "P1 does not allow"),
        ({"verdict": "accept", "severity": "P2"}, "only modify_brief"),
        ({"verdict": "reject", "severity": "P2", "brief_fix": ""}, "P2 does not allow"),
    ],
)
def test_invalid_brief_verdicts_fail(workflow, changes, message):
    with pytest.raises(ValueError, match=message):
        validate_review(brief_verdict(workflow, **changes), [workflow], {})


@pytest.mark.parametrize("outcome", ["accept", "revise", "reject"])
def test_old_review_bytes_still_round_trip(workflow, outcome):
    old = verdict(workflow, verdict=outcome, reasons=["Original finding"]).model_dump()
    assert "severity" not in old["tasks"][0] and "brief_fix" not in old["tasks"][0]
    parsed = Review.model_validate(old)
    validate_review(parsed, [workflow], {})
    assert parsed.model_dump() == old


def test_only_the_brief_changes(workflow):
    original = workflow.model_dump()
    original["feature_cell"] = {"collections": ["app.orders", "app.parts"], "decision": "release"}
    readings = brief_verdict(workflow).model_dump()
    before = deepcopy((original, readings))
    fixed, effective = apply_brief_fixes([original], readings)
    assert fixed[0].pop("brief") == FIX
    assert json.dumps(fixed[0]) == json.dumps({k: v for k, v in original.items() if k != "brief"})
    assert (original, readings) == before
    assert effective["tasks"][0]["verdict"] == "accept"


@pytest.mark.parametrize(
    "brief",
    [
        "   ",
        "Do it tomorrow.",
        "Record evidence, reconcile the handoff, and verify provenance.",
        " ".join(["word"] * 30) + ".",
    ],
)
def test_failed_gate_keeps_original_and_requests_revision(workflow, brief):
    original = workflow.model_dump()
    readings = brief_verdict(workflow, brief_fix=brief).model_dump()
    fixed, effective = apply_brief_fixes([original], readings)
    assert fixed == [original]
    assert effective["tasks"][0]["verdict"] == "revise"
    assert "plain-English check" in effective["tasks"][0]["reasons"][-1]
    assert readings["tasks"][0]["verdict"] == "modify_brief"


def test_gate_checks_unchanged_title_and_deliverables(workflow):
    for changes in ({"title": "Evidence handoff"}, {"deliverables": ["Send it tomorrow"]}):
        original = {**workflow.model_dump(), **changes}
        fixed, effective = apply_brief_fixes([original], brief_verdict(workflow).model_dump())
        assert fixed == [original]
        assert effective["tasks"][0]["verdict"] == "revise"


def test_fixture_measurement_one_revision_becomes_zero(root, company, workflow, monkeypatch):
    workflow.brief = "Develop it and check that it is ready to build."
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["generation"]["max_revisions"] = 1
    before_state = deepcopy(state)

    class WordingCritic(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            return brief_verdict(workflow), receipt

    monkeypatch.setattr(pipeline, "Models", WordingCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    snapshot = pipeline.review_snapshot(root, directory, state, company.id, None)
    reviewed = pipeline.review_candidate(directory, snapshot)
    legacy = deepcopy(reviewed)
    task = legacy[1]["review"]["tasks"][0]
    task["verdict"] = "revise"
    task.pop("brief_fix")
    task.pop("severity")
    pipeline.publish(root, directory, before_state, company.id, None, reviewed=legacy)
    assert before_state["jobs"][company.id]["revisions"] == 1
    assert before_state["jobs"][company.id]["status"] == "queued"

    source_bytes = (path / "workflows.json").read_bytes()
    Critic.calls = 0
    for _ in range(2):
        pipeline.publish(root, directory, state, company.id, None, reviewed=reviewed)
    assert state["jobs"][company.id].get("revisions", 0) == 0
    assert state["jobs"][company.id]["status"] == "reviewed"
    assert len(state["entries"]) == 1
    assert state["entries"][0]["verdict"] == "accept"
    published = read(root / state["entries"][0]["workflow_path"])
    assert published == {**workflow.model_dump(), "brief": FIX}
    assert (path / "workflows.json").read_bytes() == source_bytes
    assert read(path / "review.json") == reviewed[1]
    assert Critic.calls == 0


@pytest.mark.parametrize("budget", [0, 1])
def test_pipeline_gate_failure_honors_revision_budget(root, company, workflow, monkeypatch, budget):
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["generation"]["max_revisions"] = budget

    class BadWording(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            return brief_verdict(workflow, brief_fix="Do it tomorrow."), receipt

    monkeypatch.setattr(pipeline, "Models", BadWording)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    job = state["jobs"][company.id]
    assert job.get("revisions", 0) == budget
    assert read(path / "workflows.json") == [workflow.model_dump()]
    assert all(e["verdict"] != "accept" for e in state["entries"])
    if budget:
        assert job["status"] == "queued"
        assert "plain-English check" in job["feedback"]
    else:
        assert job["status"] == "reviewed"


def test_other_task_revision_preserves_the_brief_fix(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    second = workflow.model_copy(update={"id": "example_second", "outline_id": "second"})
    company.outlines.append(company.outlines[0].model_copy(update={"id": "second"}))
    write(path / "company.json", company.model_dump())
    write(path / "workflows.json", [workflow.model_dump(), second.model_dump()])

    class MixedCritic(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            result = brief_verdict(workflow)
            result.tasks += verdict(
                second, verdict="revise", reasons=["Existing review needs a design repair"]
            ).tasks
            return result, receipt

    def expand(models, prompt, dossier, evidence, selected, feedback):
        assert selected == ["second"]
        return [second], {"model": "codex/author"}

    monkeypatch.setattr(pipeline, "Models", MixedCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    monkeypatch.setattr(pipeline.workflow_job, "expand", expand)
    pipeline.publish(root, directory, state, company.id, None)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert read(path / "workflows.json") == [{**workflow.model_dump(), "brief": FIX}, second.model_dump()]


@pytest.mark.parametrize("outcome", ["revise", "reject"])
def test_company_verdict_still_blocks_acceptance(root, company, workflow, monkeypatch, outcome):
    directory, state, _ = setup_job(root, company, workflow)

    class CompanyCritic(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            result = brief_verdict(workflow)
            result.company_verdict = outcome
            result.company_reasons = ["The firm's work lacks captured evidence."]
            return result, receipt

    monkeypatch.setattr(pipeline, "Models", CompanyCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    assert not state["entries"]
    assert state["jobs"][company.id]["status"] == ("queued" if outcome == "revise" else "rejected")


def test_brief_fix_cannot_bypass_new_neighbor_conflict(root, company, workflow, monkeypatch):
    directory, state, _ = setup_job(root, company, workflow)
    state["config"]["generation"]["max_revisions"] = 0

    class WordingCritic(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            return brief_verdict(workflow), receipt

    monkeypatch.setattr(pipeline, "Models", WordingCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    reviewed = pipeline.review_candidate(
        directory, pipeline.review_snapshot(root, directory, state, company.id, None)
    )
    neighbor = {**workflow.model_dump(), "id": "other_release"}
    write(root / "neighbor.json", neighbor)
    state["entries"].append(
        {
            "workflow_id": neighbor["id"],
            "workflow_path": "neighbor.json",
            "verdict": "accept",
            "family_id": neighbor["id"],
        }
    )
    monkeypatch.setattr(
        pipeline,
        "nearest",
        lambda *args: [{"workflow_id": neighbor["id"], "workflow": neighbor, "similarity": 0.99}],
    )
    pipeline.publish(root, directory, state, company.id, None, reviewed=reviewed)
    assert all(e["workflow_id"] != workflow.id for e in state["entries"])
    assert state["jobs"][company.id]["admission_conflicts"]
