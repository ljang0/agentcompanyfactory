import base64
import json
from datetime import datetime
from types import SimpleNamespace

from test_teacher import Script, ScriptedBackend, company  # noqa: F401

from company_envs.storage import read, write
from company_envs.world import teacher


def test_ordinary_trial_never_receives_private_reference(company):  # noqa: F811
    root, folder = company
    write(
        folder / "tasks/task/golden.json",
        [{"worker_id": "boss", "action": "message", "text": "REFERENCE_ONLY_763"}],
    )
    model = Script()
    result = teacher.teacher_rollout(
        root,
        folder,
        "task",
        backend=ScriptedBackend(),
        budgets={"seconds": 5, "model_calls": 5},
        models=model,
        trial_kind="ordinary",
    )
    assert model.calls and all("REFERENCE_ONLY_763" not in prompt for prompt in model.calls)
    assert not result["reference_informed"] and result["class"] == "ordinary_failed"
    assert (folder / "runtime/ordinary/task/TRIAL.json").exists()
    assert not (folder / "runtime/teacher/task/TEACHER.json").exists()
    assert "teacher_class" not in read(folder / "tasks/task/STATUS.json")


def test_worker_ablation_omits_specialist_without_assigning_a_substitute(company):  # noqa: F811
    root, folder = company
    result = teacher.teacher_rollout(
        root,
        folder,
        "task",
        backend=ScriptedBackend(),
        budgets={"seconds": 5, "model_calls": 5},
        models=Script(),
        trial_kind="worker-ablation",
        unavailable_worker="peer",
    )
    assert result["unavailable_worker"] == "peer" and not result["reference_informed"]
    assert set(result["trajectory_summary"]["episode"]["workers"]) == {"boss"}


def test_staged_teacher_keeps_late_decisions_in_long_reference(company):  # noqa: F811
    folder = company[1]
    write(folder / "world/POPULATION.json", {"reference_date": "2026-09-01T08:00:00-07:00"})
    write(
        folder / "tasks/task/golden.json",
        [
            {
                "worker_id": "boss",
                "action": "state",
                "app_id": "docs",
                "state_patch": {
                    "documents": [{"id": "decision", "content": "x" * 12000 + "FINAL_DECISION_941"}]
                },
            },
            {"worker_id": "peer", "action": "message", "text": "PEER_PRIVATE_126"},
        ],
    )
    note = teacher.insider_note(folder, "task", "boss")
    assert "FINAL_DECISION_941" in note and "PEER_PRIVATE_126" not in note


def test_guest_clock_preserves_reference_offset_and_records_measurement(tmp_path):
    reference = "2026-09-01T08:00:00-07:00"
    target = datetime.fromisoformat(reference).timestamp()
    write(tmp_path / "world/POPULATION.json", {"reference_date": reference})
    calls = []

    class Transport:
        async def run(self, command, *, deadline):
            calls.append(command)
            return {
                "exit_code": 0,
                "stdout": base64.b64encode(
                    json.dumps(
                        {
                            "before_epoch": target + 172800,
                            "after_epoch": target + 0.1,
                        }
                    ).encode()
                ).decode(),
            }

    backend = teacher._LiveBackend(tmp_path, tmp_path)
    backend.folder, backend.roster = tmp_path, ["boss", "peer"]
    backend.backend = lambda worker: SimpleNamespace(transport=Transport())
    backend.synchronize_clocks()
    proof = read(tmp_path / "runtime/vms/CLOCK.json")
    assert proof["target_epoch"] == target and set(proof["workers"]) == {"boss", "peer"}
    assert all(str(target) in command and "set-ntp" in command for command in calls)


def test_quiesce_retains_actual_native_snapshot_before_reset(tmp_path, monkeypatch):
    from company_envs.world import hub_vm

    stopped = []
    monkeypatch.setattr(hub_vm, "stop_company", lambda folder: stopped.append(folder))
    backend = teacher._LiveBackend(tmp_path, tmp_path)
    backend.folder, backend.world = tmp_path, SimpleNamespace(sid="trial")
    write(tmp_path / "runtime/sessions.json", {"sid": "trial"})
    backend.clients = {"app": teacher._MemoryClient({"records": [{"id": "one"}]})}
    backend.clients["app"].current["records"][0]["decision"] = "approved"
    backend.quiesce()
    proof = read(tmp_path / "runtime/NATIVE-FINAL.json")
    assert stopped == [tmp_path] and proof["status"] == "captured"
    assert proof["apps"]["app"]["current_state"]["records"][0]["decision"] == "approved"


def test_scored_cap_counts_regraded_episode_once_and_excludes_unavailable(tmp_path):
    from company_envs.world.staged_trials import scored_episodes

    trials = tmp_path / "runtime/trials/task/teacher"
    for number, classification in enumerate(
        ("environment_error", "verification_incomplete", "teacher_failed", "verification_incomplete"), 1
    ):
        write(
            trials / f"{number:02d}/RESULT.json",
            {"result": {"class": classification, "score": 0.0, "trajectory_runtime": f"runtime/run{number}"}},
        )
    for version in ("old", "new"):
        write(
            tmp_path / f"runtime/run4/grades/task/regraded/{version}/RESULT.json",
            {"class": "teacher_passed", "score": 1.0},
        )
    assert scored_episodes(tmp_path, trials, "teacher") == 2
