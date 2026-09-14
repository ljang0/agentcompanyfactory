from company_envs import pipeline
from company_envs.models import ModelUnavailable
from company_envs.report import build_report
from company_envs.schemas import Candidate, Review
from company_envs.storage import digest, read, write


def setup_job(root, company, workflow):
    directory = pipeline.new_run(root, 1, 1, workers=1)
    state = read(directory / "run.json")
    state["config"]["models"]["review_policy"] = "different_model"
    state["config"]["design"]["agency"] = False  # Legacy-path coverage; agency has separate tests.
    for flag in (
        "feature_matrix",
        "read_only_tools",
        "select_outlines",
        "allow_amendment",
        "review_separate_witness",
        "minimum_workers",
        "execution_mode",
        "available_runtime_apps",
    ):
        state["config"]["design"].pop(flag, None)
    state["config"]["generation"].pop("isolate_model_failures", None)
    state["config"]["design"]["workflow_version"] = "1"
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        sector=company.sector,
        website=company.website,
        reason="Test research candidate",
    )
    job = {
        "id": company.id,
        "candidate": candidate.model_dump(),
        "status": "drafted",
        "coverage": {},
        "expansion_count": 1,
    }
    state["jobs"][job["id"]] = job
    path = directory / "jobs" / job["id"]
    write(path / "company.json", company.model_dump())
    write(path / "workflows.json", [workflow.model_dump()])
    write(
        path / "authors.json",
        {"research": "codex/author", "expand": "codex/author", "receipts": [{"model": "codex/author"}]},
    )
    text = "We manufacture instruments."
    evidence = [
        {
            "claim_id": "operations",
            "kind": "sourced",
            "source_url": company.website,
            "status": "supported",
            "text_hash": digest(text),
            "quote": text,
            "capture_error": "",
        }
    ]
    write(path / "evidence.json", evidence)
    write(
        root / "data" / "sources" / f"{digest(company.website)}.json",
        {"url": company.website, "status": "captured", "text": text, "text_hash": digest(text)},
    )
    write(directory / "run.json", state)
    return directory, state, path


class Critic:
    calls = 0

    def __init__(self, config, directory):
        pass

    def call(self, job, prompt, response_type, avoid=(), **kwargs):
        assert "codex/author" in avoid
        Critic.calls += 1
        return Review(
            company_verdict="accept",
            company_reasons=[],
            tasks=[
                {
                    "workflow_id": "example_release",
                    "verdict": "accept",
                    "quality": 4,
                    "novelty": "distinct",
                    "duplicate_of": "",
                    "reasons": [],
                }
            ],
        ), {"model": "claude/critic", "status": "complete", "prompt_hash": digest(prompt)}


def test_publish_resume_and_tampering(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    Critic.calls = 0
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    assert build_report(root, directory, state)["counts"]["workflows"] == 1
    pipeline.publish(root, directory, state, "example", None)
    assert Critic.calls == 1 and len(state["entries"]) == 1
    write(root / "unrelated.json", {"docs": "changed"})
    assert build_report(root, directory, state)["status"] == "complete"
    raw = read(path / "workflows.json")
    raw[0]["decision_problem"] = "Changed after review"
    write(path / "workflows.json", raw)
    result = build_report(root, directory, state)
    assert result["status"] == "incomplete" and result["validation_errors"]


def test_model_unavailability_does_not_admit_design(root, company, workflow, monkeypatch):
    directory, _state, _path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "prepare", lambda *args: "example")

    def fail(*args):
        raise ModelUnavailable("Test provider quota exhausted")

    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    monkeypatch.setattr(pipeline, "review_candidate", fail)
    result = pipeline.run(root, directory)
    assert result["status"] == "incomplete"
    assert result["jobs"]["example"]["status"] == "pending_model"
    assert read(directory / "dataset.json")["counts"]["workflows"] == 0


def test_missing_review_fails_closed(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    (path / "review.json").rename(path / "review-moved.json")
    report = build_report(root, directory, state)
    assert report["counts"]["workflows"] == 0


def test_seed_is_frozen_and_resume_cannot_shrink(root):
    directory = pipeline.new_run(root, 3, 6, seed=42)
    assert read(directory / "run.json")["config"]["generation"]["seed"] == 42
    import pytest

    with pytest.raises(ValueError, match="never shrink"):
        pipeline.run(root, directory, companies=1, tasks=2)


def test_changed_reference_catalog_excludes_design(root, company, workflow, monkeypatch):
    directory, state, _path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    write(directory / "catalogs" / "apps.json", {"apps": []})
    report = build_report(root, directory, state)
    assert report["counts"]["workflows"] == 0
    assert "catalogs" in report["validation_errors"]


def test_captured_source_tampering_excludes_design(root, company, workflow, monkeypatch):
    directory, state, _path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    write(root / "data" / "sources" / f"{digest(company.website)}.json", {"text": "Changed source"})
    report = build_report(root, directory, state)
    assert report["counts"]["workflows"] == 0


def test_review_revision_has_a_hard_limit(root, company, workflow, monkeypatch):
    directory, state, _path = setup_job(root, company, workflow)
    state["config"]["generation"]["max_revisions"] = 1  # the limit under test, whatever config.toml says

    class Reviser(Critic):
        def call(self, *args, **kwargs):
            review, receipt = super().call(*args, **kwargs)
            review.tasks[0].verdict = "revise"
            review.tasks[0].reasons = ["Needs a substantive downstream decision"]
            return review, receipt

    monkeypatch.setattr(pipeline, "Models", Reviser)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    assert state["jobs"]["example"]["status"] == "queued"
    assert state["jobs"]["example"]["revisions"] == 1
    # Merely re-reviewing the same authored bytes does not spend another repair.
    pipeline.publish(root, directory, state, "example", None)
    assert state["jobs"]["example"]["status"] == "queued"
    revised = read(_path / "workflows.json")
    revised[0]["brief"] += " Revised attempt."
    write(_path / "workflows.json", revised)
    pipeline.publish(root, directory, state, "example", None)
    assert state["jobs"]["example"]["status"] == "reviewed"
    assert state["jobs"]["example"]["revisions"] == 1
    assert build_report(root, directory, state)["counts"]["workflows"] == 0


def test_prepare_preserves_workflows_not_requested_for_revision(root, company, workflow, monkeypatch):
    import json

    directory, state, path = setup_job(root, company, workflow)
    second = workflow.model_copy(update={"id": "example_second", "outline_id": "second"})
    company.outlines.append(company.outlines[0].model_copy(update={"id": "second"}))
    write(path / "company.json", company.model_dump())
    write(path / "workflows.json", [workflow.model_dump(), second.model_dump()])
    job = state["jobs"]["example"]
    job["feedback"] = json.dumps(
        {
            "company_verdict": "accept",
            "tasks": [
                {"workflow_id": workflow.id, "verdict": "accept"},
                {"workflow_id": second.id, "verdict": "revise"},
            ],
        }
    )

    def expand(models, prompt, dossier, evidence, selected, feedback):
        assert selected == ["second"]
        return [second.model_copy(update={"brief": "Revised brief"})], {"model": "codex/author"}

    monkeypatch.setattr(pipeline.workflow_job, "expand", expand)
    pipeline.prepare(root, directory, state, job)
    saved = read(path / "workflows.json")
    assert saved[0] == workflow.model_dump()
    assert saved[1]["brief"] == "Revised brief"


def test_coverage_counts_participants_not_decorative_roster_roles(root, company, workflow, monkeypatch):
    company.workers.append(company.workers[0].model_copy(update={"id": "recreation", "soc": "39-9032"}))
    company.teams[0].worker_ids.append("recreation")
    directory, state, _path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, "example", None)
    coverage = build_report(root, directory, state)["coverage"]
    assert "39-9032" in coverage["company_inventory_priority_occupations"]
    assert "39-9032" not in coverage["priority_occupations"]


def test_brief_fix_uses_cached_review_on_resume(root, company, workflow, monkeypatch):
    from test_critic_rewrite import FIX, brief_verdict

    directory, state, path = setup_job(root, company, workflow)
    state["config"]["generation"]["max_revisions"] = 0

    class BriefCritic(Critic):
        def call(self, *args, **kwargs):
            _, receipt = super().call(*args, **kwargs)
            return brief_verdict(workflow), receipt

    monkeypatch.setattr(pipeline, "Models", BriefCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    Critic.calls = 0
    pipeline.publish(root, directory, state, company.id, None)
    first_review = (path / "review.json").read_bytes()
    pipeline.publish(root, directory, state, company.id, None)
    assert Critic.calls == 1
    assert len(state["entries"]) == 1
    assert state["entries"][0]["verdict"] == "accept"
    assert read(root / state["entries"][0]["workflow_path"])["brief"] == FIX
    assert (path / "review.json").read_bytes() == first_review
    assert state["jobs"][company.id].get("revisions", 0) == 0
