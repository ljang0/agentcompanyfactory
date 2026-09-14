from copy import deepcopy

import pytest

from company_envs.storage import digest, write
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
