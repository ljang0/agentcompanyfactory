import json

import pytest
from test_pipeline import Critic, setup_job

from company_envs import pipeline
from company_envs.audit import audit_run
from company_envs.models import ModelOutputInvalid, Models
from company_envs.provenance import author_receipts, validate_separation
from company_envs.report import build_report
from company_envs.schemas import Review
from company_envs.skills import freeze_skills
from company_envs.storage import digest, read, write


def receipt(role, session, model="codex/gpt-6-astra"):
    return {
        "job": role,
        "model": model,
        "session_id": session,
        "call_id": session,
        "status": "complete",
        "attempt": 1,
    }


def test_skills_are_frozen_not_live_dependencies(root):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    prompts, skills = freeze_skills(root)
    assert state["prompts"] == prompts and state["skills"] == skills
    (root / skills["research"]["path"]).write_text("changed after run creation")
    assert audit_run(root, directory)["stages"]["inputs"]["status"] == "pass"
    state["skills"]["research"]["source"] += "tampered"
    assert audit_run(root, directory, state)["stages"]["inputs"]["status"] == "fail"


def test_fresh_session_is_not_different_model():
    authors = [receipt("research", "author")]
    reviewer = receipt("review", "reviewer")
    validate_separation("fresh_session", reviewer, authors)
    with pytest.raises(ValueError, match="also authored"):
        validate_separation("different_model", reviewer, authors)


@pytest.mark.parametrize(
    "field,value", [("session_id", "author"), ("call_id", "author"), ("session_id", ""), ("call_id", "")]
)
def test_fresh_review_rejects_missing_or_reused_context(field, value):
    reviewer = receipt("review", "reviewer")
    reviewer[field] = value
    with pytest.raises(ValueError):
        validate_separation("fresh_session", reviewer, [receipt("research", "author")])


def test_invalid_draft_receipt_survives_cache(root, monkeypatch):
    import company_envs.models as adapter

    def execute(cmd, prompt, directory, timeout):
        write(directory / "answer.json", {"invalid": True})
        (directory / "stdout.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": "draft"}))
        return 0, 1.0

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"expand": ["codex/gpt-6-astra"], "reasoning": "high"},
    }
    for _ in range(2):
        with pytest.raises(ModelOutputInvalid) as error:
            Models(config, root).call("expand", "prompt", Review)
        assert error.value.receipt["session_id"] == "draft"


def test_research_repair_keeps_failed_source_author(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    (path / "company.json").unlink()
    (path / "authors.json").unlink()
    bad = company.model_copy(deep=True)
    bad.evidence[0].quote = "A paraphrase absent from the captured source."
    drafts = iter(
        [
            (bad, receipt("research", "first", "codex/gpt-first")),
            (company, receipt("research_repair", "repair")),
        ]
    )
    monkeypatch.setattr(pipeline.research_job, "research", lambda *args, **kwargs: next(drafts))
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert {r["session_id"] for r in author_receipts(path)} == {"first", "repair"}


def test_historical_attempt_prevents_false_independence(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    write(path / "research-attempt-0-0.json", {"receipt": {"model": "claude/critic"}})
    report = build_report(root, directory, state)
    assert report["counts"]["workflows"] == 0
    assert "also authored" in report["validation_errors"][workflow.id]
    assert audit_run(root, directory, state)["stages"]["review"]["status"] == "fail"


def test_all_astra_publish_and_read_only_audit(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["models"]["review_policy"] = "fresh_session"
    write(path / "authors.json", {"receipts": [receipt("research", "r"), receipt("expand", "e")]})

    class AstraCritic(Critic):
        def call(self, job, prompt, response_type, avoid=(), **kwargs):
            assert not avoid
            verdict, _ = super().call(job, prompt, response_type, avoid={"codex/author"})
            return verdict, {**receipt("review", "v"), "prompt_hash": digest(prompt)}

    monkeypatch.setattr(pipeline, "Models", AstraCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    write(directory / "run.json", state)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = audit_run(root, directory)
    after = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert result["status"] == "pass" and before == after
    data = read(path / "authors.json")
    data["receipts"][0]["session_id"] = "v"
    write(path / "authors.json", data)
    assert build_report(root, directory)["counts"]["workflows"] == 0


def test_rejected_draft_findings_do_not_invalidate_selected_work(root, company, workflow):
    directory, state, path = setup_job(root, company, workflow)
    state["jobs"][company.id]["status"] = "rejected"
    (path / "evidence.json").unlink()
    result = audit_run(root, directory, state)
    assert result["stages"]["research"]["quarantined"]
    assert not result["stages"]["research"]["errors"]
    assert result["status"] == "partial"


def test_dossier_only_revision_preserves_accepted_workflows(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    job = state["jobs"][company.id]
    job.update(
        rewrite_company=True,
        feedback=json.dumps(
            {"company_verdict": "revise", "tasks": [{"workflow_id": workflow.id, "verdict": "accept"}]}
        ),
    )
    revised = company.model_copy(deep=True)
    revised.assumptions.append("Narrowed factual attribution.")
    monkeypatch.setattr(
        pipeline.research_job,
        "research",
        lambda *a, **k: (revised, receipt("research_repair", "dossier-revision")),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("A valid accepted workflow must not be reauthored for a dossier-only correction")

    monkeypatch.setattr(pipeline.workflow_job, "expand", forbidden)
    before = (path / "workflows.json").read_bytes()
    pipeline.prepare(root, directory, state, job)
    assert (path / "workflows.json").read_bytes() == before
    assert read(path / "company.json")["assumptions"] == revised.assumptions


def test_full_captures_are_deduplicated_and_include_distant_support(root, company):
    from company_envs.sources import Sources

    text = "Press-brake forming is available. " + "other capability " * 500 + "We manufacture instruments."
    write(
        root / "data/sources" / f"{digest(company.website)}.json", {"text": text, "text_hash": digest(text)}
    )
    company.evidence.append(company.evidence[0].model_copy(update={"id": "other"}))
    pages = Sources(root).excerpts(company)
    assert len(pages) == 1 and pages[0]["claim_ids"] == ["operations", "other"]
    assert pages[0]["excerpt"] == text


def test_changed_evidence_packet_invalidates_review_cache(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    Critic.calls = 0
    pipeline.publish(root, directory, state, company.id, None)
    monkeypatch.setattr(pipeline.Sources, "excerpts", lambda *args: [{"excerpt": "different support packet"}])
    pipeline.publish(root, directory, state, company.id, None)
    assert Critic.calls == 2
    assert len(list((path / "review-history").glob("*.json"))) == 1


def test_interrupted_call_leaves_receipt(root, monkeypatch):
    import company_envs.models as adapter

    def execute(cmd, prompt, directory, timeout):
        assert (directory / "receipt.json").exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(adapter, "execute", execute)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"review": ["codex/gpt-6-astra"], "reasoning": "high"},
    }
    with pytest.raises(KeyboardInterrupt):
        Models(config, root).call("review", "prompt", Review)
    receipts = list(root.glob("calls/*/attempt-*/receipt.json"))
    assert len(receipts) == 1 and read(receipts[0])["status"] == "interrupted"
