from types import SimpleNamespace

import pytest

from company_envs.storage import write
from company_envs.world import staged_grading as grading
from company_envs.world.python_verifier import VerifierUnavailable


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    initial = {"app": {"records": [{"id": 1, "status": "open"}]}}
    final = {"app": {"records": [{"id": 1, "status": "closed"}]}}
    assessment = {
        "criteria": [{"criterion": 1, "weight": 1, "must_pass": True, "rubric": "Resolve the issue"}],
        "dependencies": [{"worker_id": "producer", "consumed_by": ["consumer"]}],
    }
    task = tmp_path / "tasks/task"
    write(
        task / "workflow.json",
        {"success_criteria": [{"method": "state", "requirement": "Resolve the issue"}]},
    )
    write(task / "assignment.json", {"brief": "Resolve the issue using the specialist's diagnosis."})
    monkeypatch.setattr(grading, "require_task_world", lambda *args: None)
    monkeypatch.setattr(grading, "proof_key", lambda *args: "proof")
    monkeypatch.setattr(grading, "_assessment", lambda *args: assessment)
    monkeypatch.setattr(grading, "full_initial_states", lambda *args: initial)
    monkeypatch.setattr(grading, "assessment_sources", lambda *args: {"app.policy#7": {"allowed": True}})
    monkeypatch.setattr(
        grading, "run_verifier", lambda *args: {"checks": {"1": {"passed": True, "reason": "Status closed"}}}
    )

    def judge(models, base, item, **kwargs):
        return SimpleNamespace(status="pass", reason="Relevant outcome", evidence_refs=["changes"]), None, []

    monkeypatch.setattr(grading, "_judge_one", judge)
    data = {
        "initial": initial,
        "final": final,
        "files": {},
        "events": [
            {"worker_id": "producer", "sequence": 1, "changes": {"diagnosis": "cause"}},
            {"worker_id": "consumer", "sequence": 2, "changes": {"resolution": "fixed cause"}},
        ],
    }
    return tmp_path, data


def test_no_semantic_models_means_incomplete_even_with_good_mechanics(evaluation):
    root, data = evaluation
    report = grading.evaluate(root, root, "task", data)
    assert not report["passed"] and not report["complete"]


def test_missing_snapshot_is_unavailable_before_any_credit(evaluation):
    root, data = evaluation
    data["final"] = {}
    with pytest.raises(VerifierUnavailable, match="every app"):
        grading.evaluate(root, root, "task", data, models=object())


def test_missing_collection_is_an_evidence_gap(evaluation):
    root, data = evaluation
    data["final"]["app"] = {}
    with pytest.raises(VerifierUnavailable, match="missing native"):
        grading.evaluate(root, root, "task", data, models=object())


def test_high_score_does_not_hide_missing_contributor(evaluation):
    root, data = evaluation
    data["events"] = data["events"][1:]
    report = grading.evaluate(root, root, "task", data, models=object())
    assert report["score"] == 1 and report["complete"] and not report["passed"]
    assert report["dependencies"][0]["status"] == "fail"


def test_consumer_work_before_producer_is_insufficient(evaluation):
    root, data = evaluation
    data["events"][0]["sequence"] = 3
    report = grading.evaluate(root, root, "task", data, models=object())
    assert not report["passed"]


def test_mechanical_failure_cannot_be_overridden_by_semantic_pass(evaluation, monkeypatch):
    root, data = evaluation
    monkeypatch.setattr(
        grading, "run_verifier", lambda *args: {"checks": {"1": {"passed": False, "reason": "Wrong record"}}}
    )
    report = grading.evaluate(root, root, "task", data, models=object())
    assert report["complete"] and not report["passed"] and report["score"] == 0


def test_unchanged_policy_and_actor_evidence_reach_judge(evaluation, monkeypatch):
    root, data = evaluation

    def judge(models, base, item, **kwargs):
        assert any(e.get("reference") == "app.policy#7" and e["current"]["allowed"] for e in item["evidence"])
        assert any(e["id"] == "actors" and e["current"] == data["events"] for e in item["evidence"])
        return SimpleNamespace(status="pass", reason="Supported", evidence_refs=["changes"]), None, []

    monkeypatch.setattr(grading, "_judge_one", judge)
    report = grading.evaluate(root, root, "task", data, models=object())
    assert report["passed"] and report["complete"]


def test_oversized_complete_evidence_is_unavailable_instead_of_truncated(evaluation, monkeypatch):
    root, data = evaluation
    data["events"] = []
    data["final"]["app"]["records"][0]["content"] = "business evidence " * 1000
    monkeypatch.setattr(grading, "JUDGMENT_INPUT_LIMIT", 10000)

    def unexpected_judge(*args, **kwargs):
        pytest.fail("Oversized evidence must not reach the legacy truncating judge")

    monkeypatch.setattr(grading, "_judge_one", unexpected_judge)
    with pytest.raises(VerifierUnavailable, match="after lossless encoding"):
        grading.evaluate(root, root, "task", data, models=object())
