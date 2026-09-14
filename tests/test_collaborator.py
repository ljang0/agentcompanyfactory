import json
from pathlib import Path

import pytest

from company_envs.collaborator import _json, portable_metadata, verify_checkout
from company_envs.storage import digest, read


def test_portable_metadata_rebinds_both_file_and_object_hashes():
    review = {"verdict": "accept", "profile": "/home/person/.codex", "cwd": "/home/person/work"}
    review_bytes = _json(review)
    checkpoint = {"review_bytes": digest(review_bytes), "review_object": digest(review)}
    frozen = {"checkpoint": digest(checkpoint)}
    source = {
        "review.json": review_bytes,
        "checkpoint.json": _json(checkpoint),
        "frozen.json": _json(frozen),
        "world/app.state.json": b'{"records":[{"id":7}]}',
    }
    result, changes = portable_metadata(
        source, repository=Path("/home/person/work"), user_home=Path("/home/person")
    )
    updated = json.loads(result["checkpoint.json"])
    assert updated["review_bytes"] == digest(result["review.json"])
    assert updated["review_object"] == digest(json.loads(result["review.json"]))
    assert json.loads(result["frozen.json"])["checkpoint"] == digest(updated)
    assert result["world/app.state.json"] == source["world/app.state.json"]
    assert b"/home/person" in source["review.json"] and b"/home/person" not in result["review.json"]
    assert set(changes) == {"review.json", "checkpoint.json", "frozen.json"}


def test_packaging_refuses_to_redact_canonical_business_records():
    with pytest.raises(ValueError, match="business records"):
        portable_metadata(
            {"world/app.state.json": _json({"content": "/home/person/customer work"})},
            repository=Path("/home/person/repo"),
            user_home=Path("/home/person"),
        )


def test_clean_checkout_verification_refuses_tampered_payload_before_live_work(tmp_path):
    from company_envs.storage import write

    (tmp_path / "code.py").write_text("altered")
    write(tmp_path / "COLLABORATOR.json", {"files": {"code.py": digest(b"original")}})
    with pytest.raises(ValueError, match="payload changed"):
        verify_checkout(tmp_path, work=tmp_path / "work")
    assert not (tmp_path / "work").exists()


def test_jsonl_path_redaction_preserves_event_order_and_rebinds_file_hash():
    events = b'{"sequence": 1, "path": "/home/person/repo/result"}\n{"sequence": 2}\n'
    files = {"events.jsonl": events, "receipt.json": _json({"events_hash": digest(events)})}
    exported, _ = portable_metadata(
        files, repository=Path("/home/person/repo"), user_home=Path("/home/person")
    )
    rows = [json.loads(line) for line in exported["events.jsonl"].splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["path"] == "<repository>/result"
    assert json.loads(exported["receipt.json"])["events_hash"] == digest(exported["events.jsonl"])


def test_package_refuses_an_environment_failure_even_with_reset(tmp_path, monkeypatch):
    from company_envs.collaborator import require_team_evidence
    from company_envs.storage import write
    from company_envs.world import staged_grading, task_assessment

    write(tmp_path / "MANIFEST.json", {"tasks": ["task"]})
    write(
        tmp_path / "runtime/teacher/task/TEACHER.json",
        {
            "class": "environment_error",
            "simulation_only": False,
            "reset": {"status": "pass"},
        },
    )
    monkeypatch.setattr(task_assessment, "require_task_world", lambda *args: {})
    monkeypatch.setattr(staged_grading, "proof_key", lambda *args: "proof")
    with pytest.raises(ValueError, match="successful real trial"):
        require_team_evidence(tmp_path, tmp_path)


def test_package_uses_explicit_repaired_grade_and_requires_business_drop(tmp_path, monkeypatch):
    from company_envs.collaborator import require_team_evidence
    from company_envs.storage import write
    from company_envs.world import staged_grading, task_assessment

    monkeypatch.setattr(task_assessment, "require_task_world", lambda *args: {})
    monkeypatch.setattr(staged_grading, "proof_key", lambda *args: "proof")
    write(tmp_path / "MANIFEST.json", {"tasks": ["task"]})
    write(
        tmp_path / "tasks/task/assessment.json",
        {
            "dependencies": [
                {"worker_id": "boss", "inputs": []},
                {"worker_id": "peer", "inputs": ["app.records#7"]},
            ]
        },
    )
    for kind in ("teacher", "ordinary", "worker-ablation", "input-ablation"):
        name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
        write(
            tmp_path / "runtime" / kind / "task" / name,
            {
                "class": kind + "_passed",
                "simulation_only": False,
                "reset": {"status": "pass"},
                "grade_report": f"runtime/regraded/{kind}/report.json",
                "trajectory_runtime": f"runtime/{kind}/saved",
                "trajectory_summary": {
                    "episode": {
                        "workers": {"boss": {}} if kind == "worker-ablation" else {"boss": {}, "peer": {}}
                    }
                },
                "unavailable_worker": "peer" if kind == "worker-ablation" else None,
                "withheld_inputs": {"worker_id": "peer", "references": ["app.records#7"]}
                if kind == "input-ablation"
                else None,
            },
        )
        write(
            tmp_path / f"runtime/regraded/{kind}/report.json",
            {"proof_key": "proof", "complete": True, "passed": True, "score": 1.0},
        )
    write(
        tmp_path / "runtime/input-ablation/saved/INPUT-ABLATION.json",
        {"status": "view_probe_passed", "worker_id": "peer", "references": ["app.records#7"]},
    )
    with pytest.raises(ValueError, match="weaker business results"):
        require_team_evidence(tmp_path, tmp_path)
    write(
        tmp_path / "runtime/regraded/worker-ablation/report.json",
        {"proof_key": "proof", "complete": True, "passed": False, "score": 0.75},
    )
    assert set(require_team_evidence(tmp_path, tmp_path)["task"]) == {
        "teacher",
        "ordinary",
        "worker-ablation",
        "input-ablation",
    }
    worker_path = tmp_path / "runtime/worker-ablation/task/TRIAL.json"
    worker_report = read(worker_path)
    write(worker_path, {**worker_report, "unavailable_worker": None})
    with pytest.raises(ValueError, match="exactly its named worker"):
        require_team_evidence(tmp_path, tmp_path)
    write(worker_path, worker_report)
    write(tmp_path / "runtime/input-ablation/saved/INPUT-ABLATION.json", {"status": "unmeasured"})
    with pytest.raises(ValueError, match="measured input-ablation view check"):
        require_team_evidence(tmp_path, tmp_path)


def test_screenshot_dedup_survives_archive_extraction_without_linking_editable_source(tmp_path):
    import tarfile

    from company_envs.collaborator import write_payload

    files = {
        "company/runtime/run/trace/screen-1.png": b"identical screenshot",
        "company/runtime/run/trace/transport/duplicate.png": b"identical screenshot",
        "src/first.py": b"same source",
        "src/second.py": b"same source",
    }
    output = tmp_path / "checkout"
    result = write_payload(output, files)
    assert result["duplicate_screenshot_bytes_shared"] == len(b"identical screenshot")
    assert (output / "src/first.py").stat().st_ino != (output / "src/second.py").stat().st_ino
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(output, arcname="checkout")
    extracted = tmp_path / "extracted"
    with tarfile.open(archive) as stream:
        stream.extractall(extracted, filter="data")
    assert {name: (extracted / "checkout" / name).read_bytes() for name in files} == files


def test_inspection_keeps_unavailable_and_failed_trials_without_claiming_success(tmp_path, monkeypatch):
    from company_envs.collaborator import inspection_evidence
    from company_envs.storage import write
    from company_envs.world import staged_grading, task_assessment

    monkeypatch.setattr(task_assessment, "require_task_world", lambda *args: {})
    monkeypatch.setattr(staged_grading, "proof_key", lambda *args: "current")
    write(tmp_path / "MANIFEST.json", {"tasks": ["task"]})
    write(tmp_path / "tasks/task/golden.json", {"steps": []})
    calibration = {"status": "accepted", "proof_key": "current", "golden_hash": digest({"steps": []})}
    write(tmp_path / "tasks/task/verifier-calibration.json", calibration)
    for kind, outcome in [("teacher", "teacher_failed"), ("ordinary", "model_unavailable")]:
        name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
        write(
            tmp_path / f"runtime/{kind}/task/{name}",
            {
                "class": outcome,
                "score": 0.0,
                "simulation_only": False,
                "reset": {"status": "pass"},
                "trajectory_runtime": f"runtime/{kind}/saved",
            },
        )
    result = inspection_evidence(tmp_path, tmp_path)
    assert result["task"]["teacher"]["receipt"]["class"] == "teacher_failed"
    assert result["task"]["ordinary"]["receipt"]["class"] == "model_unavailable"
    assert result["task"]["ordinary"]["grade"] is None
    write(tmp_path / "tasks/task/verifier-calibration.json", {**calibration, "proof_key": "stale"})
    with pytest.raises(ValueError, match="current accepted calibration"):
        inspection_evidence(tmp_path, tmp_path)
    write(tmp_path / "tasks/task/verifier-calibration.json", calibration)
    path = tmp_path / "runtime/ordinary/task/TRIAL.json"
    write(path, {**read(path), "grade_report": "../outside.json"})
    with pytest.raises(ValueError, match="within the company"):
        inspection_evidence(tmp_path, tmp_path)


def test_inspection_replay_records_a_distinct_noncertifying_proof(tmp_path, monkeypatch):
    from company_envs import collaborator
    from company_envs.storage import write
    from company_envs.world import python_verifier, staged_calibration

    company = tmp_path / "example"
    write(company / "MANIFEST.json", {"tasks": ["task"]})
    write(company / "world/acceptance/runtime/BROWSER.json", {"status": "pass"})
    write(company / "runtime/old-trial.json", {"status": "retained"})
    write(company / "tasks/task/workflow.json", {"success_criteria": [{"method": "state"}]})
    write(tmp_path / "COLLABORATOR.json", {"mode": "inspection", "company": "example", "files": {}})
    calls = []
    monkeypatch.setattr(collaborator, "inspection_evidence", lambda *args: calls.append("inspection"))

    def reject_full_acceptance(*args):
        raise ValueError("team gates incomplete")

    monkeypatch.setattr(collaborator, "require_team_evidence", reject_full_acceptance)

    def replay(root, folder, task, work):
        assert not (folder / "runtime").exists()
        assert read(folder / "world/acceptance/runtime/BROWSER.json") == {"status": "pass"}
        data = {"initial": {}, "final": {"completed": True}}
        reference = folder / "runtime/verifier" / task / "references/checkpoint"
        write(reference / "REFERENCE.json", {"status": "replayed_and_reset", "data_hash": digest(data)})
        write(reference / "data.json", data)
        calls.append("replay")
        return data

    monkeypatch.setattr(staged_calibration, "reference_replay", replay)
    monkeypatch.setattr(python_verifier, "run_verifier", lambda *args: {"checks": {"1": {"passed": True}}})
    result = verify_checkout(tmp_path, work=tmp_path / "work")
    assert calls == ["inspection", "replay"]
    assert result["status"] == "inspection_verified_not_published"
    assert result["certified"] is False and result["model_calls"] == 0
    assert len(result["proof_files"]) == 2
    for name, expected in result["proof_files"].items():
        assert digest((tmp_path / name).read_bytes()) == expected
    write(tmp_path / "COLLABORATOR.json", {"mode": "validated", "company": "example", "files": {}})
    with pytest.raises(ValueError, match="team gates incomplete"):
        verify_checkout(tmp_path, work=tmp_path / "other-work")


def test_inspection_index_labels_provider_failure_as_unscored():
    from company_envs.collaborator import inspection_index

    trials = {
        "task": {
            "ordinary": {
                "receipt": {
                    "class": "model_unavailable",
                    "score": 0.0,
                    "trajectory_runtime": "runtime/ordinary/run",
                },
                "grade": None,
            }
        }
    }
    result = inspection_index("example", trials, []).decode()
    assert "business score unscored" in result
    assert "business score 0.0" not in result
