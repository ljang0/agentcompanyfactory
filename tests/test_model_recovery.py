import subprocess

from company_envs import pipeline
from company_envs.models import ModelUnavailable, failure_policy, provider_failure
from company_envs.storage import read, write


def test_transient_retry_budget_and_shared_failures(root):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    state["jobs"]["one"] = {"status": "running"}
    transient = ModelUnavailable("timeout", retryable=True)
    assert not pipeline.model_failure(state, "one", transient)
    assert state["jobs"]["one"]["status"] == "queued"
    assert not pipeline.model_failure(state, "one", transient)
    assert state["jobs"]["one"]["status"] == "pending_model"
    assert len(state["jobs"]["one"]["model_failure_history"]) == 2
    assert pipeline.model_failure(state, "one", ModelUnavailable("auth", global_failure=True))
    assert state["error"] == "auth"


def test_legacy_failure_policy_still_stops(root):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    state["config"]["generation"].pop("isolate_model_failures")
    state["jobs"]["one"] = {}
    assert pipeline.model_failure(state, "one", ModelUnavailable("timeout", retryable=True))
    assert state["jobs"]["one"]["status"] == "pending_model"


def test_failure_classification_is_conservative():
    assert failure_policy(subprocess.TimeoutExpired("codex", 1)) == (True, False)
    assert failure_policy(ValueError("bad schema")) == (False, False)
    assert failure_policy(provider_failure("HTTP 503 temporarily unavailable")) == (True, False)
    assert failure_policy(provider_failure("unauthorized: invalid API key")) == (False, True)
    assert failure_policy(provider_failure("unknown failure")) == (False, False)


def test_a_prompt_over_the_input_ceiling_is_never_retried():
    """The refusal is permanent but arrives dressed as transport, which read as transient.

    One macerich authoring call retried 162 times on this, each attempt failing in under half a
    second, because the message contains "turn/start failed". The size is the caller's to fix.
    """
    oversize = (
        "Error: turn/start: turn/start failed: Input exceeds the maximum length of 1048576 "
        'characters. (code -32602), data: {"input_error_code":"input_too_large"}'
    )
    assert failure_policy(provider_failure(oversize)) == (False, False)
    assert not provider_failure(oversize).busy  # nor is it worth waiting out as a busy provider
    # The same transport wrapper without the size complaint is still retried.
    assert failure_policy(provider_failure("Error: turn/start: turn/start failed")) == (True, False)


def test_one_failed_job_does_not_stop_other_jobs(root, monkeypatch):
    directory = pipeline.new_run(root, 2, 2, workers=1)
    state = read(directory / "run.json")
    state["jobs"] = {name: {"id": name, "status": "queued"} for name in ("bad", "good")}
    write(directory / "run.json", state)
    attempted = []

    def prepare(root, run_dir, state, job):
        attempted.append(job["id"])
        if job["id"] == "bad":
            raise ModelUnavailable("timeout", retryable=True)

    def publish(root, run_dir, state, jid, encoder, reviewed=None):
        state["jobs"][jid]["status"] = "reviewed"

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(pipeline, "publish", publish)
    monkeypatch.setattr(
        pipeline, "review_snapshot", lambda root, directory, state, jid, encoder: directory / f"{jid}.json"
    )
    monkeypatch.setattr(pipeline, "review_candidate", lambda *args: None)
    monkeypatch.setattr(pipeline, "fill_queue", lambda *args: False)
    monkeypatch.setattr(pipeline, "coverage", lambda *args: {"companies": 0, "tasks": 0, "sectors": {}})
    monkeypatch.setattr("company_envs.report.export", lambda *args: {"status": "incomplete"})
    result = pipeline.run(root, directory)
    assert attempted.count("bad") == 2
    assert attempted.count("good") == 1
    assert result["jobs"]["bad"]["status"] == "pending_model"
    assert result["jobs"]["good"]["status"] == "reviewed"


def test_a_retry_does_not_get_a_fresh_call_budget(tmp_path):
    """A budget is per unit of work, and a retry is the same unit.

    `Models` is constructed inside `seed_world`, so counting only this process's calls gave each of
    the driver's three attempts a fresh allowance: one company reached 900 calls against a budget of
    500, and the 23 companies past it hold a third of all compute with 12 of them producing nothing.
    """
    from company_envs.models import Models, dispatched_calls

    run = tmp_path / "world"
    for call in ("aaa", "bbb"):
        (run / "calls" / call / "attempt-001").mkdir(parents=True)
    (run / "calls" / "bbb" / "attempt-002").mkdir()
    assert dispatched_calls(run) == 3

    config = {"models": {"world_states": ["codex/gpt-test"], "reasoning": "high"}, "generation": {}}
    assert Models(config, run, max_calls=10).calls_spent == 0, "a caller that did not ask is unchanged"
    assert Models(config, run, max_calls=10, cumulative=True).calls_spent == 3
    # Uncapped work is never counted: there is nothing to count against.
    assert Models(config, run, cumulative=True).calls_spent == 0
    # A budget already spent refuses the next call rather than starting over.
    spent = Models(config, run, max_calls=3, cumulative=True)
    assert spent.calls_spent >= spent.max_calls


def test_the_seed_asks_for_its_budget_to_be_cumulative():
    import inspect

    from company_envs.world import state_seed

    source = inspect.getsource(state_seed.seed_world)
    assert "cumulative=True" in source
    assert "seed_call_budget" in source
