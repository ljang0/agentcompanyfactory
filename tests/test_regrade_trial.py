import pytest

from company_envs.storage import read, write
from company_envs.world import regrade_trial


def test_environment_failure_cannot_be_promoted(tmp_path):
    write(tmp_path / "runtime/trials/task/teacher/01/RESULT.json", {"result": {"class": "environment_error"}})
    with pytest.raises(ValueError, match="environment failure"):
        regrade_trial.regrade_trial(tmp_path, tmp_path, "task", kind="teacher", attempt=1)


def test_regrading_preserves_original_episode_and_writes_explicit_new_grade(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime/teacher/task/runs/one/company/runtime"
    checkpoint = tmp_path / "runtime/trials/task/teacher/01/RESULT.json"
    original = {
        "class": "verification_incomplete",
        "simulation_only": False,
        "reset": {"status": "pass"},
        "budget": {"episode_reason": "budget_exhausted"},
        "threshold": 1.0,
        "evidence_refs": [],
    }
    write(checkpoint, {"result": original})
    for name in ("assignment.json", "workflow.json", "assessment.json", "WORLD.json"):
        write(tmp_path / "tasks/task" / name, {"unchanged": name})
        write(runtime.parent / "tasks/task" / name, {"unchanged": name})
    write(
        tmp_path / "tasks/task/verifier-calibration.json",
        {"status": "accepted", "proof_key": "repaired-proof"},
    )
    monkeypatch.setattr(
        regrade_trial,
        "collect_saved_trial",
        lambda *args, **kwargs: (original, runtime, {"actual": "retained"}),
    )
    monkeypatch.setattr(regrade_trial, "proof_key", lambda *args: "repaired-proof")
    monkeypatch.setattr(regrade_trial, "_models", lambda *args: None)
    monkeypatch.setattr(
        regrade_trial,
        "evaluate",
        lambda *args, **kwargs: {
            "score": 1.0,
            "passed": True,
            "complete": True,
            "evidence": {},
            "proof_key": "repaired-proof",
        },
    )
    result = regrade_trial.regrade_trial(tmp_path, tmp_path, "task", kind="teacher", attempt=1)
    assert result["class"] == "teacher_passed"
    assert read(checkpoint)["result"] == original
    assert read(tmp_path / result["grade_report"])["proof_key"] == "repaired-proof"
    assert read(tmp_path / "runtime/teacher/task/TEACHER.json") == result
