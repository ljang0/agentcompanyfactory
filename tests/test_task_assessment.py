from types import SimpleNamespace

import pytest

from company_envs.schemas import TaskAssessment
from company_envs.storage import write
from company_envs.world.task_assessment import indexed_records, validate_assessment
from company_envs.world.task_author import seeded_snapshot


def assessment_fixture():
    workers = ["lead", "technician", "reviewer"]
    documents = {w: {"id": w, "ownerId": w, "sharedWith": []} for w in workers}
    state = {"documents": documents}
    assessment = TaskAssessment.model_validate(
        {
            "criteria": [
                {
                    "criterion": 1,
                    "weight": 1,
                    "must_pass": True,
                    "evidence": ["docs.documents#lead"],
                    "rubric": "The disposition follows the inspection and applicable policy.",
                    "failure_cases": ["A claim of inspection without a supporting result"],
                }
            ],
            "dependencies": [
                {
                    "worker_id": w,
                    "inputs": [f"docs.documents#{w}"],
                    "produces": "A supported decision within the assigned responsibility",
                    "consumed_by": [workers[(i + 1) % 3]],
                    "necessity": "The consumer needs this interpretation to decide the disposition.",
                    "authority_sources": [],
                }
                for i, w in enumerate(workers)
            ],
            "alternative_successes": ["Record a supported decision to defer"],
            "prohibited_shortcuts": ["One worker impersonating the other contributors"],
        }
    )
    task = SimpleNamespace(
        assessment=assessment,
        success_criteria=[{}],
        worker_ids=workers,
        contributions=[SimpleNamespace(worker_id=w, apps=["docs"]) for w in workers],
    )
    snapshot = {
        "states": {"docs": state},
        "worker_apps": {w: ["docs"] for w in workers},
        "identities": {w: {"docs": {"id": w}} for w in workers},
    }
    return task, snapshot


def test_private_worker_inputs_are_validated_against_real_views():
    task, snapshot = assessment_fixture()
    assert validate_assessment(task, snapshot)["workers"] == 3
    task.assessment.dependencies[0].inputs = ["docs.documents#technician"]
    with pytest.raises(ValueError, match="inaccessible"):
        validate_assessment(task, snapshot)


def test_apps_without_user_collections_use_canonical_worker_identity():
    task, snapshot = assessment_fixture()
    for document in snapshot["states"]["docs"]["documents"].values():
        document.pop("ownerId")
    snapshot["states"]["docs"]["emails"] = [
        {"id": f"mail-{w}", "to": f"{w}@example.test"} for w in task.worker_ids
    ]
    for dependency in task.assessment.dependencies:
        dependency.inputs.append(f"docs.emails#mail-{dependency.worker_id}")
    snapshot["world"] = {
        "staff": [{"id": w, "name": w, "email": f"{w}@example.test"} for w in task.worker_ids]
    }
    snapshot["identity_keys"] = {"docs": None}
    snapshot["identities"] = {w: {} for w in task.worker_ids}
    assert validate_assessment(task, snapshot)["workers"] == 3
    snapshot["identity_keys"]["docs"] = "currentUser"
    with pytest.raises(ValueError, match="Missing app identity"):
        validate_assessment(task, snapshot)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_worker",
        "self_consumer",
        "missing_criterion",
        "unknown_authority",
        "wider_apps",
        "no_assessment",
    ],
)
def test_invalid_assessments_fail_before_authoring_verifiers(defect):
    task, snapshot = assessment_fixture()
    if defect == "missing_worker":
        task.assessment.dependencies.pop()
    elif defect == "self_consumer":
        task.assessment.dependencies[0].consumed_by = ["lead"]
    elif defect == "missing_criterion":
        task.assessment.criteria = []
    elif defect == "unknown_authority":
        task.assessment.dependencies[0].authority_sources = ["docs.documents#missing-policy"]
    elif defect == "wider_apps":
        task.contributions[0].apps.append("admin")
    else:
        task.assessment = None
    with pytest.raises(ValueError):
        validate_assessment(task, snapshot)


def test_native_index_retains_slack_primary_ids():
    value = {
        "room": [
            {"messageId": "question", "content": "Please inspect the seal."},
            {"messageId": "answer", "content": "The seal is damaged."},
        ]
    }
    assert [rid for rid, _ in indexed_records("messages", value)] == ["question", "answer"]
    assert next(indexed_records("documents", {"doc-key": {"title": "Inspection"}}))[0] == "doc-key"


def test_assessment_weights_and_must_pass_are_independent():
    from company_envs.world.grader_core import assessment_score

    assessment = {
        "criteria": [
            {"criterion": 1, "weight": 9, "must_pass": False},
            {"criterion": 2, "weight": 1, "must_pass": True},
        ]
    }
    checks = [
        {"criterion_ref": "/success_criteria/0", "status": "pass"},
        {"criterion_ref": "/success_criteria/1", "status": "fail"},
    ]
    result = assessment_score(assessment, checks)
    assert result["score"] == 0.9 and result["complete"] and not result["must_pass_satisfied"]
    # Adding an equivalent check cannot increase that criterion's weight.
    assert assessment_score(assessment, checks + [checks[0]])["score"] == 0.9
    checks[1]["status"] = "pending"
    result = assessment_score(assessment, checks)
    assert not result["complete"] and not result["must_pass_satisfied"]
    checks[1]["status"] = "pass"
    result = assessment_score(assessment, checks)
    assert result["score"] == 1 and result["complete"] and result["must_pass_satisfied"]
    with pytest.raises(ValueError, match="do not match"):
        assessment_score(assessment, checks[:1])


def test_assessment_changes_invalidate_grading_context(tmp_path):
    from company_envs.storage import digest
    from company_envs.world.grader_core import _grading_context

    task, _ = assessment_fixture()
    workflow = {"assessment": task.assessment.model_dump()}
    folder = tmp_path / "tasks/resolve"
    write(folder / "workflow.json", workflow)
    write(folder / "assessment.json", workflow["assessment"])
    original = digest(_grading_context(tmp_path, "resolve"))
    workflow["assessment"]["criteria"][0]["weight"] = 5
    write(folder / "workflow.json", workflow)
    with pytest.raises(ValueError, match="differs"):
        _grading_context(tmp_path, "resolve")
    write(folder / "assessment.json", workflow["assessment"])
    assert digest(_grading_context(tmp_path, "resolve")) != original


def test_assessment_sources_keep_complete_unchanged_policy_records():
    from company_envs.world.task_assessment import assessment_sources

    task, snapshot = assessment_fixture()
    policy = snapshot["states"]["docs"]["documents"]["lead"]
    policy["content"] = "Only the lead can authorize reopening the service case."
    sources = assessment_sources(task.assessment.model_dump(), snapshot["states"])
    assert sources["docs.documents#lead"] == policy
    assert set(sources) == {f"docs.documents#{w}" for w in task.worker_ids}
    del snapshot["states"]["docs"]["documents"]["lead"]
    with pytest.raises(ValueError, match="Missing"):
        assessment_sources(task.assessment.model_dump(), snapshot["states"])


def test_staged_discovery_refuses_unfrozen_world_before_reading_apps(tmp_path):
    write(tmp_path / "world/CORE.json", {})
    with pytest.raises(ValueError, match="Stage 4"):
        seeded_snapshot(tmp_path, root=tmp_path)


def test_staged_snapshot_uses_effective_world_and_excludes_runtime_logs(tmp_path, monkeypatch):
    from company_envs.world import world_acceptance

    calls = []
    monkeypatch.setattr(world_acceptance, "accepted_world", lambda root, folder: calls.append(folder))
    for relative in [
        "company.json",
        "world/CORE.json",
        "world/FROZEN.json",
        "world/identities.json",
        "world/worker_apps.json",
        "world/acceptance/REVIEW.json",
        "world/acceptance/RUNTIME.json",
    ]:
        write(tmp_path / relative, {})
    write(
        tmp_path / "apps.json",
        {
            "apps": [
                {"app_id": "docs", "state_file": "world/docs.state.json", "top_level_keys": ["documents"]}
            ]
        },
    )
    write(
        tmp_path / "world/docs.state.json",
        {"documents": {"d": {"id": "d", "title": "A revised working note"}}},
    )
    write(tmp_path / "world/world.json", {"context": "original core"})
    write(tmp_path / "world/population/EFFECTIVE-WORLD.json", {"context": "approved additions"})
    write(
        tmp_path / "world/POPULATION.json",
        {"effective_world": "world/population/EFFECTIVE-WORLD.json", "artifacts": {}},
    )
    write(tmp_path / "world/population/call-log.json", {"unrelated": "debug output"})
    first = seeded_snapshot(tmp_path, root=tmp_path)
    assert first["world"] == {"context": "approved additions"} and first["accepted_world"]
    write(tmp_path / "world/population/call-log.json", {"unrelated": "new debug output"})
    assert seeded_snapshot(tmp_path, root=tmp_path)["provenance"] == first["provenance"]
    assert len(calls) == 2
