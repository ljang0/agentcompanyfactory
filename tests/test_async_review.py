import copy
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from test_pipeline import setup_job

from company_envs import pipeline
from company_envs.report import build_report
from company_envs.schemas import Review
from company_envs.storage import digest, read, write


def setup_async_jobs(root, company, workflow, monkeypatch, count=4, workers=4):
    directory, state, original = setup_job(root, company, workflow)
    template = state["jobs"].pop(company.id)
    state.pop("portfolio", None)
    state["targets"] = {"companies": count, "tasks": count}
    state["config"]["generation"]["workers"] = workers
    state["config"]["models"].update(review_policy="fresh_session", review=["codex/gpt-test"])
    for index in range(count):
        cid = f"company-{index}"
        dossier = company.model_copy(update={"id": cid, "real_firm": f"Firm {index}"})
        design = workflow.model_copy(
            update={"id": f"{cid}_release", "company_id": cid, "canonical_description": f"design-{index}"}
        )
        job = copy.deepcopy(template)
        job.update(id=cid, status="queued")
        job["candidate"].update(id=cid, real_firm=dossier.real_firm)
        state["jobs"][cid] = job
        path = directory / "jobs" / cid
        write(path / "company.json", dossier.model_dump())
        write(path / "workflows.json", [design.model_dump()])
        write(path / "evidence.json", read(original / "evidence.json"))
        write(
            path / "authors.json",
            {"receipts": [{"model": "codex/gpt-test", "call_id": f"author-{cid}", "session_id": f"a-{cid}"}]},
        )
    write(directory / "run.json", state)
    monkeypatch.setattr(pipeline, "prepare", lambda root, directory, state, job: job["id"])
    monkeypatch.setattr(pipeline, "fill_queue", lambda *args: False)
    encoder = SimpleNamespace(encode=lambda text: np.eye(8)[int(text.rsplit("-", 1)[1])])
    monkeypatch.setattr(pipeline, "Embeddings", lambda *args: encoder)
    return directory, state, encoder


def fake_reviews(monkeypatch, delay=0, before=None):
    """Exercise real model caching, prompt construction, receipts and session separation."""
    lock = threading.Lock()
    observed = {"calls": [], "active": 0, "peak": 0}

    def execute(cmd, prompt, directory, timeout, **kwargs):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        with lock:
            observed["active"] += 1
            observed["peak"] = max(observed["peak"], observed["active"])
            observed["calls"].append({"payload": payload, "thread": threading.get_ident()})
        try:
            if before:
                before(payload)
            time.sleep(delay)
            write(
                directory / "answer.json",
                Review(
                    company_verdict="accept",
                    company_reasons=[],
                    tasks=[
                        {
                            "workflow_id": w["id"],
                            "verdict": "accept",
                            "quality": 4,
                            "novelty": "distinct",
                            "duplicate_of": "",
                            "reasons": [],
                        }
                        for w in payload["workflows"]
                    ],
                ).model_dump(),
            )
            (directory / "stdout.jsonl").write_text(
                json.dumps({"type": "thread.started", "thread_id": f"critic-{digest(prompt)}"}) + "\n"
            )
            return 0, delay
        finally:
            with lock:
                observed["active"] -= 1

    monkeypatch.setattr("company_envs.models.execute", execute)
    return observed


def test_async_review_speed_and_serialized_admission(root, company, workflow, monkeypatch):
    coordinator = threading.get_ident()
    original_write, original_publish = pipeline.write, pipeline.publish
    admissions = []

    def checked_write(path, value):
        if path.name == "run.json" or "data" in path.parts:
            assert threading.get_ident() == coordinator
        original_write(path, value)

    def checked_publish(root, directory, state, jid, encoder, reviewed=None):
        assert threading.get_ident() == coordinator
        count = len(state["entries"])
        original_publish(root, directory, state, jid, encoder, reviewed=reviewed)
        assert len(state["entries"]) == count + 1
        admissions.append(jid)

    monkeypatch.setattr(pipeline, "write", checked_write)
    monkeypatch.setattr(pipeline, "publish", checked_publish)
    # Isolate the 0.2s review latency from schema/audit rendering. Real publication,
    # atomic checkpoints and the full report validation below remain exercised.
    monkeypatch.setattr(
        "company_envs.report.export",
        lambda root, directory, state: {
            "status": "complete" if len(state["entries"]) == state["targets"]["tasks"] else "incomplete"
        },
    )
    elapsed = {}
    for width in (1, 4):
        directory, _, _ = setup_async_jobs(root, company, workflow, monkeypatch, workers=width)
        observed = fake_reviews(monkeypatch, delay=0.2)
        started = time.monotonic()
        result = pipeline.run(root, directory)
        elapsed[width] = time.monotonic() - started
        assert result["status"] == "complete"
        assert observed["peak"] == width
        assert len(observed["calls"]) == 4
        assert all(call["thread"] != coordinator for call in observed["calls"])
        assert len(result["entries"]) == len({e["workflow_id"] for e in result["entries"]}) == 4
        assert read(directory / "run.json") == result
        report = build_report(root, directory)
        assert not report["validation_errors"]
        assert report["counts"]["companies"] == report["counts"]["workflows"] == 4
    print(
        f"Review benchmark: serial={elapsed[1]:.3f}s parallel={elapsed[4]:.3f}s speedup={elapsed[1] / elapsed[4]:.2f}x"
    )
    assert len(admissions) == 8
    assert elapsed[4] < elapsed[1] * 0.65


def test_late_duplicate_is_revised_before_admission(root, company, workflow, monkeypatch):
    directory, _, _ = setup_async_jobs(root, company, workflow, monkeypatch, count=2, workers=2)
    duplicate_path = directory / "jobs" / "company-1" / "workflows.json"
    duplicate = read(duplicate_path)
    duplicate[0]["canonical_description"] = "design-0"
    write(duplicate_path, duplicate)
    # Both first reviews must start before either job can be admitted.
    barrier = threading.Barrier(2)

    def before(payload):
        if payload["workflows"][0]["canonical_description"] == "design-0":
            barrier.wait(timeout=5)

    observed = fake_reviews(monkeypatch, before=before)
    repairs = []

    def prepare(root, directory, state, job):
        if job.get("feedback"):
            repairs.append(job["id"])
            verdict = json.loads(job["feedback"])
            assert verdict["tasks"][0]["verdict"] == "revise"
            assert "Admission conflict" in verdict["tasks"][0]["reasons"][-1]
            assert all(e["job_id"] != job["id"] for e in state["entries"])
            path = directory / "jobs" / job["id"] / "workflows.json"
            designs = read(path)
            designs[0]["canonical_description"] = "design-2"
            write(path, designs)
        return job["id"]

    monkeypatch.setattr(pipeline, "prepare", prepare)
    result = pipeline.run(root, directory)
    assert result["status"] == "complete"
    assert len(repairs) == 1
    repaired = result["jobs"][repairs[0]]
    assert repaired["revisions"] == 1
    assert len(repaired["revision_bases"]) == len(repaired["admission_conflicts"]) == 1
    conflict = repaired["admission_conflicts"][0]["conflicts"][0]
    assert conflict["neighbors"][0]["similarity"] == pytest.approx(1)
    assert len(observed["calls"]) == 3
    assert not build_report(root, directory)["validation_errors"]
    # The coordinator's conflict never edits the independent review or its receipt.
    history = list((directory / "jobs" / repaired["id"] / "review-history").glob("*.json"))
    assert len(history) == 1
    assert read(history[0])["review"]["tasks"][0]["verdict"] == "accept"


@pytest.mark.parametrize("crash_at", ["model_cached", "before_admission", "after_admission"])
def test_async_review_resume_uses_snapshot_and_cached_call(root, company, workflow, monkeypatch, crash_at):
    directory, _, _ = setup_async_jobs(root, company, workflow, monkeypatch, count=2, workers=2)
    # Synchronize the first calls so both snapshots predate either admission.
    barrier = threading.Barrier(2)
    observed = fake_reviews(monkeypatch, before=lambda payload: barrier.wait(timeout=5))
    original_publish, original_review = pipeline.publish, pipeline.review
    interrupted = False
    admitted = []

    def interrupt_publish(root, directory, state, jid, encoder, reviewed=None):
        nonlocal interrupted
        if crash_at == "before_admission" and admitted and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        original_publish(root, directory, state, jid, encoder, reviewed=reviewed)
        admitted.append(jid)
        if crash_at == "after_admission" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    def interrupt_review(*args, **kwargs):
        nonlocal interrupted
        result = original_review(*args, **kwargs)
        if crash_at == "model_cached" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(pipeline, "publish", interrupt_publish)
    monkeypatch.setattr(pipeline, "review", interrupt_review)
    first = pipeline.run(root, directory)
    assert first["status"] == "incomplete"
    pending = [j for j in first["jobs"].values() if j["status"] == "drafted"]
    assert pending
    snapshots = {j["id"]: j["review_snapshot"] for j in pending}
    assert len(observed["calls"]) == 2
    monkeypatch.setattr(pipeline, "publish", original_publish)
    monkeypatch.setattr(pipeline, "review", original_review)
    monkeypatch.setattr(pipeline, "prepare", lambda *args: pytest.fail("resume must not reauthor"))
    resumed = pipeline.run(root, directory)
    assert resumed["status"] == "complete"
    assert len(observed["calls"]) == 2
    assert len(resumed["entries"]) == len({e["workflow_id"] for e in resumed["entries"]}) == 2
    assert all(resumed["jobs"][jid]["review_snapshot"] == path for jid, path in snapshots.items())
    assert all(job.get("revisions", 0) == 0 for job in resumed["jobs"].values())
    assert len(list(directory.glob("calls/*/attempt-*/receipt.json"))) == 2
    assert not build_report(root, directory)["validation_errors"]


def test_review_snapshot_is_immutable_and_changed_candidate_cannot_be_admitted(
    root, company, workflow, monkeypatch
):
    directory, state, encoder = setup_async_jobs(root, company, workflow, monkeypatch, count=1)
    observed = fake_reviews(monkeypatch)
    snapshot_path = pipeline.review_snapshot(root, directory, state, "company-0", encoder)
    path = directory / "jobs" / "company-0" / "workflows.json"
    changed = read(path)
    changed[0]["brief"] = "Changed after snapshot"
    write(path, changed)
    reviewed = pipeline.review_candidate(directory, snapshot_path)
    assert observed["calls"][0]["payload"]["workflows"][0]["brief"] == workflow.brief
    with pytest.raises(ValueError, match="candidate changed during review"):
        pipeline.publish(root, directory, state, "company-0", encoder, reviewed=reviewed)
    assert state["entries"] == []


@pytest.mark.parametrize("budget", [0, 1])
def test_admission_conflict_budget_preserves_other_tasks(root, company, workflow, monkeypatch, budget):
    directory, state, encoder = setup_async_jobs(root, company, workflow, monkeypatch, count=2)
    state["config"]["generation"]["max_revisions"] = budget
    path = directory / "jobs" / "company-1"
    dossier = read(path / "company.json")
    dossier["outlines"].append({**dossier["outlines"][0], "id": "second"})
    write(path / "company.json", dossier)
    designs = read(path / "workflows.json")
    designs[0]["canonical_description"] = "design-0"
    designs.append(
        {**designs[0], "id": "company-1_second", "outline_id": "second", "canonical_description": "design-2"}
    )
    write(path / "workflows.json", designs)
    fake_reviews(monkeypatch)
    reviewed = {
        jid: pipeline.review_candidate(
            directory, pipeline.review_snapshot(root, directory, state, jid, encoder)
        )
        for jid in state["jobs"]
    }
    pipeline.publish(root, directory, state, "company-0", encoder, reviewed=reviewed["company-0"])
    for _ in range(2):
        pipeline.publish(root, directory, state, "company-1", encoder, reviewed=reviewed["company-1"])
    job = state["jobs"]["company-1"]
    assert job.get("revisions", 0) == budget
    assert len(job["admission_conflicts"]) == 1
    assert all(e["workflow_id"] != "company-1_release" for e in state["entries"])
    if budget:
        assert job["status"] == "queued"
        feedback = json.loads(job["feedback"])
        assert [t["verdict"] for t in feedback["tasks"]] == ["revise", "accept"]
        assert len(job["revision_bases"]) == 1
    else:
        assert job["status"] == "reviewed"
        assert {e["workflow_id"] for e in state["entries"]} == {"company-0_release", "company-1_second"}
        assert not build_report(root, directory, state)["validation_errors"]


def test_exhausted_discovery_still_reviews_active_authors(root, company, workflow, monkeypatch):
    directory, state, _ = setup_async_jobs(root, company, workflow, monkeypatch, count=2, workers=4)
    state["targets"] = {"companies": 3, "tasks": 3}
    write(directory / "run.json", state)
    observed = fake_reviews(monkeypatch)
    result = pipeline.run(root, directory)
    assert result["status"] == "incomplete"
    assert len(observed["calls"]) == len(result["entries"]) == 2
    assert all(job["status"] == "reviewed" for job in result["jobs"].values())
    assert not build_report(root, directory)["validation_errors"]


@pytest.mark.parametrize("after_admission", [False, True])
def test_crash_without_final_checkpoint_replays_admission_once(
    root, company, workflow, monkeypatch, after_admission
):
    directory, _, _ = setup_async_jobs(root, company, workflow, monkeypatch, count=1)
    observed = fake_reviews(monkeypatch)
    original_publish, original_write = pipeline.publish, pipeline.write
    crashed = False

    def interrupt_publish(*args, **kwargs):
        nonlocal crashed
        if after_admission:
            original_publish(*args, **kwargs)
        crashed = True
        raise SystemExit("simulated process crash")

    def no_final_checkpoint(path, value):
        # Model/job artifacts survive, but process-local admissions do not.
        if crashed and path == directory / "run.json":
            return
        original_write(path, value)

    monkeypatch.setattr(pipeline, "publish", interrupt_publish)
    monkeypatch.setattr(pipeline, "write", no_final_checkpoint)
    with pytest.raises(SystemExit, match="simulated process crash"):
        pipeline.run(root, directory)
    saved = read(directory / "run.json")
    assert saved["jobs"]["company-0"]["status"] == "drafted"
    assert saved["entries"] == []
    assert len(list(directory.glob("jobs/*/review-results/*.json"))) == 1

    monkeypatch.setattr(pipeline, "publish", original_publish)
    monkeypatch.setattr(pipeline, "write", original_write)
    monkeypatch.setattr(pipeline, "prepare", lambda *args: pytest.fail("resume must not reauthor"))
    for _ in range(2):
        resumed = pipeline.run(root, directory)
        assert resumed["status"] == "complete"
        assert len(resumed["entries"]) == len(resumed["companies"]) == 1
        assert resumed["jobs"]["company-0"].get("revisions", 0) == 0
        assert len(observed["calls"]) == 1
        assert read(directory / "run.json") == resumed
        assert not build_report(root, directory)["validation_errors"]
