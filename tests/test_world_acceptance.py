from copy import deepcopy

import pytest

from company_envs.storage import digest, read, write
from company_envs.world.world_acceptance import validate_runtime_evidence


def fixture(tmp_path, monkeypatch):
    from company_envs.world import hub_app, runtime_acceptance, world_acceptance

    monkeypatch.setattr(runtime_acceptance, "implementation_snapshot", lambda root: {"code": "v1"})
    monkeypatch.setattr(world_acceptance, "load_config", lambda root: {"design": {"hub_root": str(tmp_path)}})
    monkeypatch.setattr(hub_app, "source_hash", lambda path: "native-source")
    write(tmp_path / "world/worker_apps.json", {"alice": ["app"], "bob": ["app"]})
    proof = tmp_path / "proof.json"
    proof.write_text("actual evidence")
    runtime = {
        "implementation": {"code": "v1"},
        "rendered": [{"app": "app", "worker": w, "ok": True} for w in ["alice", "bob"]],
        "access": [{"app": "app", "worker": w, "ok": True} for w in ["alice", "bob"]],
        "builds": {
            "app": {"ok": True, "native_source_hash": "native-source", "build": {"dist_hash": "dist"}}
        },
        "reset": {"app": {"ok": True}},
        "writes": [{"app": "app", "ok": True}],
        "evidence": {"proof.json": digest(proof.read_bytes())},
        "actors": {"app": 1},
    }
    return runtime, {"states": {"app": "state-hash"}}


def test_freeze_rejects_stale_code_missing_worker_failed_write_and_changed_proof(tmp_path, monkeypatch):
    runtime, snapshot = fixture(tmp_path, monkeypatch)
    validate_runtime_evidence(tmp_path, tmp_path, runtime, snapshot)
    for key, value in [
        ("implementation", {"code": "old"}),
        ("rendered", runtime["rendered"][:1]),
        ("writes", [{"app": "app", "ok": False}]),
        ("evidence", {}),
    ]:
        bad = deepcopy(runtime)
        bad[key] = value
        with pytest.raises(ValueError):
            validate_runtime_evidence(tmp_path, tmp_path, bad, snapshot)
    (tmp_path / "proof.json").write_text("changed")
    with pytest.raises(ValueError, match="evidence changed"):
        validate_runtime_evidence(tmp_path, tmp_path, runtime, snapshot)


def frozen_task(tmp_path, monkeypatch):
    from company_envs.world import world_acceptance

    runtime, snapshot = fixture(tmp_path, monkeypatch)
    runtime.update(
        baseline=snapshot,
        checks=dict.fromkeys(
            ("build", "render", "writes", "persistence", "access", "resources", "reset"), True
        ),
    )
    monkeypatch.setattr(world_acceptance, "population_snapshot", lambda root, folder: snapshot)
    write(tmp_path / "world/POPULATION.json", {"reference_date": "2026-09-01"})
    write(tmp_path / "world/acceptance/REVIEW.json", {"baseline": snapshot, "review": {"verdict": "accept"}})
    frozen = world_acceptance.freeze_population(tmp_path, tmp_path, runtime)
    write(tmp_path / "world/acceptance/runtime/original/RESULT.json", runtime)
    binding = tmp_path / "tasks/task/WORLD.json"
    write(binding, {"frozen_hash": digest(frozen)})
    write(binding.parent / "verifier-calibration.json", {"status": "accepted"})
    write(tmp_path / "runtime/trials/task/teacher/01/RESULT.json", {"class": "teacher_failed"})
    return runtime, snapshot, deepcopy(frozen), binding


def test_runtime_refresh_archives_proof_and_preserves_task_and_trial_evidence(tmp_path, monkeypatch):
    from company_envs.world.world_acceptance import freeze_population

    runtime, _, frozen, binding = frozen_task(tmp_path, monkeypatch)
    assert freeze_population(tmp_path, tmp_path, runtime) == frozen
    runtime = {**runtime, "at": "new measured runtime"}
    retained = {p: p.read_bytes() for p in tmp_path.rglob("*.json") if p.name != "WORLD.json"}
    with pytest.raises(ValueError, match="--refresh-tasks"):
        freeze_population(tmp_path, tmp_path, runtime)
    refreshed = freeze_population(tmp_path, tmp_path, runtime, refresh_tasks=True)
    assert read(binding)["frozen_hash"] == digest(refreshed) != digest(frozen)
    journal_path = tmp_path / read(binding)["runtime_refresh"]
    assert read(journal_path)["bindings"] == {"tasks/task/WORLD.json": {"frozen_hash": digest(frozen)}}
    assert read(journal_path.parent / "FROZEN.json") == frozen
    assert read(journal_path.parent / "RUNTIME.json") == read(
        tmp_path / "world/acceptance/runtime/original/RESULT.json"
    )
    for path, content in retained.items():
        if path not in {tmp_path / "world/FROZEN.json", tmp_path / "world/acceptance/RUNTIME.json"}:
            assert path.read_bytes() == content


@pytest.mark.parametrize("damage", ["population", "review", "binding", "evidence", "runtime"])
def test_runtime_refresh_refuses_changed_lineage_without_rebinding(tmp_path, monkeypatch, damage):
    from company_envs.world.world_acceptance import freeze_population

    runtime, snapshot, frozen, binding = frozen_task(tmp_path, monkeypatch)
    if damage == "population":
        snapshot["states"]["app"] = "changed"
    elif damage == "review":
        review = read(tmp_path / "world/acceptance/REVIEW.json")
        write(tmp_path / "world/acceptance/REVIEW.json", {**review, "new": True})
    elif damage == "binding":
        write(binding, {"frozen_hash": "different world"})
    elif damage == "evidence":
        (tmp_path / "proof.json").write_text("tampered")
    else:
        (tmp_path / "world/acceptance/runtime/original/RESULT.json").unlink()
        write(tmp_path / "world/acceptance/RUNTIME.json", {"lost": True})
    before = binding.read_bytes()
    with pytest.raises(ValueError):
        freeze_population(tmp_path, tmp_path, {**runtime, "at": "new"}, refresh_tasks=True)
    assert binding.read_bytes() == before
    assert read(tmp_path / "world/FROZEN.json") == frozen
