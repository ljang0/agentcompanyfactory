import pytest

from company_envs import pipeline
from company_envs.models import ModelUnavailable
from company_envs.storage import read, write


def isolated_run(root, monkeypatch, job):
    directory = pipeline.new_run(root, 1, 1, workers=1)
    state = read(directory / "run.json")
    state["jobs"] = {"one": {"id": "one", **job}}
    write(directory / "run.json", state)

    def coverage(root, state, catalogs):
        complete = state["jobs"]["one"]["status"] == "reviewed"
        return {"companies": int(complete), "tasks": int(complete), "sectors": {}}

    def export(root, directory, state):
        return {"status": "complete" if state["jobs"]["one"]["status"] == "reviewed" else "incomplete"}

    monkeypatch.setattr(pipeline, "coverage", coverage)
    monkeypatch.setattr(pipeline, "fill_queue", lambda *args: False)
    monkeypatch.setattr(
        pipeline, "review_snapshot", lambda root, directory, state, jid, encoder: directory / f"{jid}.json"
    )
    monkeypatch.setattr(pipeline, "review_candidate", lambda *args: None)
    monkeypatch.setattr("company_envs.report.export", export)
    return directory


def test_review_timeout_retries_draft_without_spending_another_semantic_repair(root, monkeypatch):
    directory = isolated_run(root, monkeypatch, {"status": "queued", "feedback": "repair", "revisions": 1})
    author_calls, review_calls = [], []

    def prepare(root, directory, state, job):
        author_calls.append(job["revisions"])
        return job["id"]

    def review_candidate(directory, snapshot):
        review_calls.append(snapshot)
        if len(review_calls) == 1:
            raise ModelUnavailable("review transport timeout", retryable=True)

    def publish(root, directory, state, jid, encoder, reviewed=None):
        state["jobs"][jid].update(status="reviewed", feedback="")

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(pipeline, "review_candidate", review_candidate)
    monkeypatch.setattr(pipeline, "publish", publish)
    result = pipeline.run(root, directory)
    assert len(author_calls) == 1
    assert len(review_calls) == 2
    assert result["jobs"]["one"]["revisions"] == 1
    assert result["status"] == "complete"


@pytest.mark.parametrize("status", ["drafted", "pending_model", "error"])
def test_resumed_review_stage_never_reauthors_completed_repair(root, monkeypatch, status):
    directory = isolated_run(
        root,
        monkeypatch,
        {"status": status, "retry_stage": "review", "feedback": "repair", "revisions": 1},
    )
    author_calls = []

    def prepare(*args):
        author_calls.append(True)
        return "one"

    def publish(root, directory, state, jid, encoder, reviewed=None):
        state["jobs"][jid].update(status="reviewed", feedback="")

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(pipeline, "publish", publish)
    assert pipeline.run(root, directory)["status"] == "complete"
    assert not author_calls


def test_generic_review_error_checkpoints_the_actual_stage_for_resume(root, monkeypatch):
    directory = isolated_run(root, monkeypatch, {"status": "queued", "feedback": "repair", "revisions": 1})
    author_calls, review_calls = [], []

    def prepare(*args):
        author_calls.append(True)
        return "one"

    def publish(root, directory, state, jid, encoder, reviewed=None):
        review_calls.append(jid)
        if len(review_calls) == 1:
            raise OSError("temporary review checkpoint error")
        state["jobs"][jid].update(status="reviewed", feedback="")

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(pipeline, "publish", publish)
    first = pipeline.run(root, directory)
    assert first["status"] == "incomplete"
    assert first["jobs"]["one"]["retry_stage"] == "review"
    assert pipeline.run(root, directory)["status"] == "complete"
    assert len(author_calls) == 1
