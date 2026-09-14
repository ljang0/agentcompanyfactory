from company_envs.storage import read, write
from company_envs.world import trial_diagnostics


def test_diagnostic_success_cannot_promote_a_failed_environment(tmp_path, monkeypatch):
    folder = tmp_path / "company"
    runtime = folder / "runtime/teacher/task/runs/one/company/runtime"
    result_path = folder / "runtime/trials/task/teacher/03/RESULT.json"
    receipt = {
        "class": "environment_error",
        "score": None,
        "trajectory_runtime": str(runtime.relative_to(folder)),
        "trajectory_summary": {"episode": {"workers": {"worker": {}}}},
    }
    write(result_path, {"result": receipt})
    write(
        runtime / "NATIVE-FINAL.json",
        {
            "status": "captured",
            "errors": {},
            "apps": {
                "app": {"initial_state": {"records": []}, "current_state": {"records": [{"id": "work"}]}}
            },
        },
    )
    write(runtime / "exports/task/EXPORT.json", {"worker": {"status": "exported", "files": {}}})
    history = runtime / "evidence/history/one.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        '{"sequence":1,"sid":"real-session","worker_id":"worker","changes":{"app.records#work":{"after":{"id":"work"}}}}\n'
    )
    monkeypatch.setattr(trial_diagnostics, "proof_key", lambda *args: "current-proof")
    monkeypatch.setattr(trial_diagnostics, "_models", lambda *args: None)
    monkeypatch.setattr(
        trial_diagnostics,
        "evaluate",
        lambda *args, **kwargs: {
            "score": 1.0,
            "passed": True,
            "complete": True,
            "evidence": {},
        },
    )
    result = trial_diagnostics.diagnose_trial(tmp_path, folder, "task", kind="teacher", attempt=3)
    assert result["status"] == "diagnostic_only" and result["business_passed"]
    assert read(result_path)["result"] == receipt
    assert not (folder / "runtime/teacher/task/TEACHER.json").exists()
