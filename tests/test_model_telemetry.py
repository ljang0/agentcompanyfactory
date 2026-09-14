"""Failure receipts preserve observed work; all provider processes are doubles."""

import json
import subprocess

import pytest
from pydantic import BaseModel

from company_envs import models
from company_envs.storage import read, write


class Answer(BaseModel):
    value: str


def events(*, usage=False):
    result = [
        {"type": "thread.started", "thread_id": "observed-session"},
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "stage1",
                "tool": "read_source",
                "status": "completed",
            },
        },
        {
            "type": "item.started",
            "item": {"type": "mcp_tool_call", "server": "stage1", "tool": "read_source"},
        },
    ]
    if usage:
        result.append({"type": "turn.completed", "usage": {"input_tokens": 17, "output_tokens": 3}})
    return result


def transcript(directory, records, *, partial=False):
    text = "\n".join(json.dumps(value) for value in records) + "\n"
    (directory / "stdout.jsonl").write_text(text + ('{"type":' if partial else ""))


def backend(directory, *, job="discover"):
    return models.Models(
        {
            "generation": {"research_timeout_seconds": 1, "completion_timeout_seconds": 1},
            "models": {job: ["codex/gpt-test"], "reasoning": "high"},
        },
        directory,
    )


def test_relative_run_directory_produces_resolvable_child_paths(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.chdir(tmp_path)

    def execute(cmd, prompt, directory, timeout):
        assert directory.is_absolute()
        assert Path(cmd[cmd.index("-C") + 1]) == directory
        assert Path(cmd[cmd.index("--output-schema") + 1]).is_file()
        context_arg = next(v for v in cmd if v.startswith("mcp_servers.stage1.args="))
        assert Path(json.loads(context_arg.split("=", 1)[1])[-1]).is_file()
        write(directory / "answer.json", {"value": "ready"})
        transcript(directory, [])
        return 0, 0.01

    monkeypatch.setattr(models, "execute", execute)
    client = backend("relative/run")
    assert client.call("discover", "read saved context", Answer, context={})[0].value == "ready"
    assert list((tmp_path / "relative/run/calls").glob("*/result.json"))


def test_concurrent_instances_share_slots_and_cumulative_budget(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    active = peak = dispatched = 0
    guard = threading.Lock()

    def execute(cmd, prompt, directory, timeout, **kwargs):
        nonlocal active, peak, dispatched
        with guard:
            active += 1
            peak = max(peak, active)
            dispatched += 1
        time.sleep(0.08)
        with guard:
            active -= 1
        transcript(directory, [])
        return 1, 0.08

    monkeypatch.setattr(models, "execute", execute)
    config = backend(tmp_path).config
    instances = [models.Models(config, tmp_path, max_calls=5, cumulative=True) for _ in range(8)]

    def call(index):
        try:
            instances[index].call("discover", f"parallel {index}", Answer)
        except (models.ModelUnavailable, models.CallBudgetExhausted):
            pass

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(8)))
    assert 1 < peak <= 4
    assert dispatched == models.dispatched_calls(tmp_path) == 5


def test_a_unit_of_work_stops_when_its_call_budget_is_spent(tmp_path, monkeypatch):
    """Nothing else caps a world: the most expensive one spent 653 calls over 43 hours.

    Every dispatch counts, whether the provider answers well or badly, because the cost is paid on
    the way out. Only a cache hit is free, and a cache hit never reaches the counter.
    """
    dispatched = []

    def execute(cmd, prompt, directory, timeout, **kwargs):
        dispatched.append(prompt)
        transcript(directory, [])
        return 1, 0.01  # a clean non-answer: counted, then reported as a failed attempt

    monkeypatch.setattr(models, "execute", execute)
    budgeted = models.Models(
        {
            "generation": {"research_timeout_seconds": 1, "completion_timeout_seconds": 1},
            "models": {"discover": ["codex/gpt-test"], "reasoning": "high"},
        },
        tmp_path,
        max_calls=2,
    )
    for index in range(4):
        try:
            budgeted.call("discover", f"prompt {index}", Answer)
        except models.CallBudgetExhausted as exc:
            assert (exc.spent, exc.budget) == (2, 2)
            break
        except (ValueError, RuntimeError, OSError):
            continue  # The double never answers; only what the budget counts matters here.
    else:
        raise AssertionError("the budget never stopped anything")
    assert budgeted.calls_spent == 2, "a spent budget stops the next call, it does not count it"
    assert len(dispatched) == 2, "nothing is dispatched once the budget is gone"


def test_a_prompt_over_the_ceiling_is_refused_before_it_costs_anything(tmp_path, monkeypatch):
    """Sending it cannot succeed, so it must not be sent, retried, or recorded as an attempt.

    The provider's refusal arrives wrapped in transport wording and reads as transient, which is
    how one authoring call reached 162 attempts that each failed in under half a second.
    """
    sent = []
    monkeypatch.setattr(models, "execute", lambda *a, **k: sent.append(a))
    backend_ = backend(tmp_path)
    oversized = "x" * (models.PROMPT_CEILING + 1)

    with pytest.raises(models.PromptTooLarge) as caught:
        backend_.call("discover", oversized, Answer)

    assert caught.value.size == len(oversized)
    assert f"{models.PROMPT_CEILING:,}" in str(caught.value)
    assert not sent, "nothing may be dispatched"
    assert not list(tmp_path.glob("calls/*/attempt-*")), "no attempt directory is written"


@pytest.mark.parametrize("usage", [False, True])
def test_timeout_retains_completed_work_without_success_or_cache(tmp_path, monkeypatch, usage):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events(usage=usage), partial=True)
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(models.ModelUnavailable) as caught:
        backend(tmp_path).call("discover", "prompt", Answer)
    assert caught.value.retryable and not caught.value.global_failure
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "error" and "timed out" in receipt["error"]
    assert receipt["session_id"] == "observed-session"
    assert receipt["tool_calls"] == [{"server": "stage1", "tool": "read_source", "status": "completed"}]
    assert receipt["usage"] == ({"input_tokens": 17, "output_tokens": 3} if usage else {})
    assert receipt["cost_usd"] is None
    assert not list(tmp_path.glob("calls/*/result.json"))
    assert not list(tmp_path.glob("calls/*/invalid.json"))


def test_interruption_keeps_observed_telemetry_and_original_status(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events())
        raise KeyboardInterrupt

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(KeyboardInterrupt):
        backend(tmp_path).call("discover", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "interrupted"
    assert receipt["session_id"] == "observed-session" and len(receipt["tool_calls"]) == 1


def test_missing_answer_retains_telemetry_but_is_not_success(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events(usage=True))
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(models.ModelUnavailable):
        backend(tmp_path).call("discover", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "error" and not receipt["retryable"]
    assert receipt["session_id"] == "observed-session" and len(receipt["tool_calls"]) == 1


def test_telemetry_failure_does_not_replace_timeout(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        # No trace was created before the owned process reached its limit.
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(models.ModelUnavailable) as caught:
        backend(tmp_path).call("discover", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert caught.value.retryable and "timed out" in receipt["error"]
    assert "FileNotFoundError" in receipt["telemetry_error"]
    assert receipt["usage"] == {} and receipt["cost_usd"] is None


def test_success_and_repeated_extraction_do_not_duplicate_tools(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events(usage=True))
        write(directory / "answer.json", {"value": "actual answer"})
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    answer, receipt = backend(tmp_path).call("discover", "prompt", Answer)
    assert answer.value == "actual answer" and receipt["status"] == "complete"
    directory = next(tmp_path.glob("calls/*/attempt-001"))
    original = json.loads(json.dumps(receipt))
    models.read_codex_telemetry(directory, receipt)
    models.read_codex_telemetry(directory, receipt)
    assert receipt == original and len(receipt["tool_calls"]) == 1


def test_unexpected_tool_is_still_rejected_with_all_telemetry(tmp_path, monkeypatch):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events(usage=True))
        write(directory / "answer.json", {"value": "not trusted"})
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    # A review without a managed tool context may not use stage1 tools.
    with pytest.raises(models.ModelUnavailable, match="external tool"):
        backend(tmp_path, job="review").call("review", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "error" and receipt["session_id"] == "observed-session"
    assert len(receipt["tool_calls"]) == 1 and receipt["usage"]["input_tokens"] == 17
    assert not list(tmp_path.glob("calls/*/result.json"))


def test_a_timed_out_call_is_tried_once_more_and_separate_calls_keep_distinct_attempts(tmp_path, monkeypatch):
    attempts = []

    def execute(cmd, prompt, directory, timeout):
        attempts.append(directory.name)
        transcript(directory, events())
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(models, "execute", execute)
    model = backend(tmp_path)
    for _ in range(2):
        with pytest.raises(models.ModelUnavailable):
            model.call("discover", "prompt", Answer)
    # Each call: the first attempt times out, one retry times out, then the call fails.
    assert attempts == ["attempt-001", "attempt-002", "attempt-003", "attempt-004"]
    assert len(list(tmp_path.glob("calls/*/attempt-*/receipt.json"))) == 4
    first = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert "retrying once" in first["error"]
    assert not list(tmp_path.glob("calls/*/result.json"))


@pytest.mark.parametrize("malformed", [[], {"type": "item.completed", "item": []}])
def test_malformed_event_cannot_bypass_success_policy(tmp_path, monkeypatch, malformed):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, [malformed, *events()])
        write(directory / "answer.json", {"value": "not trusted"})
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(models.ModelUnavailable, match="malformed"):
        backend(tmp_path).call("discover", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "error" and receipt["session_id"] == "observed-session"
    assert not list(tmp_path.glob("calls/*/result.json"))


@pytest.mark.parametrize("timed_out", [False, True])
def test_nested_transcript_cannot_mask_timeout_or_pass_success(tmp_path, monkeypatch, timed_out):
    def execute(cmd, prompt, directory, timeout):
        transcript(directory, events())
        with (directory / "stdout.jsonl").open("a") as stream:
            stream.write("[" * 12000 + "]" * 12000 + "\n")
        if timed_out:
            raise subprocess.TimeoutExpired(cmd, timeout)
        write(directory / "answer.json", {"value": "not trusted"})
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    with pytest.raises(models.ModelUnavailable) as caught:
        backend(tmp_path).call("discover", "prompt", Answer)
    receipt = read(next(tmp_path.glob("calls/*/attempt-001/receipt.json")))
    assert receipt["status"] == "error" and receipt["session_id"] == "observed-session"
    assert len(receipt["tool_calls"]) == 1 and "RecursionError" in receipt["telemetry_error"]
    assert caught.value.retryable is timed_out
    assert ("timed out" if timed_out else "nested transcript") in receipt["error"]
    assert not list(tmp_path.glob("calls/*/result.json"))


def test_usage_limit_on_one_account_moves_the_call_to_the_other(tmp_path, monkeypatch):
    from pydantic import BaseModel

    class Out(BaseModel):
        text: str

    homes = []
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir()
        (home / "auth.json").write_text("{}")
        homes.append(str(home))
    seen = []

    def execute(cmd, prompt, directory, timeout, env_overrides=None):
        home = (env_overrides or {}).get("CODEX_HOME")
        seen.append(home)
        if home == homes[0]:
            (directory / "stderr.txt").write_text(
                "You've hit your usage limit. Visit https://chatgpt.com to upgrade."
            )
            (directory / "stdout.jsonl").write_text("")
            return 1, 0.1
        (directory / "stderr.txt").write_text("")
        transcript(directory, [{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}])
        (directory / "answer.json").write_text(json.dumps({"text": "ok"}))
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    monkeypatch.setattr(models, "_exhausted", {})
    monkeypatch.setattr(models, "_home_counter", iter(range(1000)))
    backend_ = models.Models(
        {
            "generation": {"research_timeout_seconds": 1, "completion_timeout_seconds": 1},
            "models": {"expand": ["codex/gpt-test"], "reasoning": "high", "codex_homes": homes},
        },
        tmp_path / "calls",
    )
    parsed, receipt = backend_.call("expand", "hello", Out)
    assert parsed.text == "ok"
    assert seen == [homes[0], homes[1]]
    assert receipt["codex_home"] == homes[1]
    # The exhausted account stays out of rotation for the cooldown.
    assert models.available_homes(backend_.config) == [homes[1]]
    _parsed2, receipt2 = backend_.call("expand", "hello again", Out)
    assert receipt2["codex_home"] == homes[1] and seen[-1] == homes[1]


@pytest.mark.parametrize("structured_error", [False, True])
def test_provider_at_capacity_is_retried_on_the_same_model(tmp_path, monkeypatch, structured_error):
    from pydantic import BaseModel

    class Out(BaseModel):
        text: str

    calls = []

    def execute(cmd, prompt, directory, timeout, env_overrides=None):
        calls.append(1)
        if len(calls) == 1:
            message = "Selected model is at capacity. Please try again later."
            (directory / "stderr.txt").write_text(
                "Reading prompt from stdin...\n" if structured_error else message
            )
            (directory / "stdout.jsonl").write_text(
                json.dumps({"type": "turn.failed", "error": {"message": message}}) if structured_error else ""
            )
            return 1, 0.1
        (directory / "stderr.txt").write_text("")
        transcript(directory, [{"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}])
        (directory / "answer.json").write_text(json.dumps({"text": "ok"}))
        return 0, 0.1

    monkeypatch.setattr(models, "execute", execute)
    monkeypatch.setattr(models, "TRANSIENT_WAIT_SECONDS", 0)
    parsed, _receipt = backend(tmp_path, job="expand").call("expand", "hello", Out)
    assert parsed.text == "ok" and len(calls) == 2


def test_exhausted_account_marker_is_shared_with_fresh_processes(tmp_path, monkeypatch):
    from pathlib import Path

    homes = [str(tmp_path / "a"), str(tmp_path / "b")]
    for h in homes:
        Path(h).mkdir()
    config = {"models": {"codex_homes": homes}}
    monkeypatch.setattr(models, "codex_homes", lambda cfg: homes)
    models.mark_exhausted(homes[0])
    assert models.available_homes(config) == [homes[1]]
    # A fresh process has an empty in-memory table but reads the marker the other process left.
    monkeypatch.setattr(models, "_exhausted", {})
    assert models.available_homes(config) == [homes[1]]
    Path(homes[0], models.EXHAUSTED_MARKER).write_text("1")  # a marker in the past no longer applies
    assert models.available_homes(config) == homes


def test_writing_a_world_gets_the_authoring_budget_not_the_completion_one():
    """Measured live: the slowest tenth of world_states calls ran past the 600s completion budget."""
    assert models.timeout_key("world_states") == "authoring_timeout_seconds"
    assert models.timeout_key("world_review") == "authoring_timeout_seconds"
    assert models.timeout_key("task_golden") == "authoring_timeout_seconds"
    assert models.timeout_key("research") == "research_timeout_seconds"
    assert models.timeout_key("expand") == "completion_timeout_seconds"
    assert models.timeout_key("worker_policy") == "completion_timeout_seconds"
