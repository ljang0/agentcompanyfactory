"""Exercise the queue with fake commands and clocks, without starting any apps."""

import importlib.util
import json
import multiprocessing
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "batch_queue.py"
spec = importlib.util.spec_from_file_location("batch_queue", SCRIPT)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


class Clock:
    def __init__(self):
        self.value = 1_800_000_000

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        time.sleep(0.001)


class Server:
    def __init__(self, dead=False, hangs=False):
        self.dead, self.hangs = dead, hangs
        self.stopped = self.killed = False

    def poll(self):
        return 1 if self.dead else None

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        if self.hangs and not self.killed:
            raise subprocess.TimeoutExpired("hub-serve", timeout)
        return 0

    def kill(self):
        self.killed = True


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = Clock()
    calls = []
    server = Server()

    def runner(cmd, log, timeout):
        args = cmd[len(queue.CLI) :]
        calls.append((args, timeout))
        command = args[0]
        if command == "export-company":
            folder = tmp_path / "companies" / args[2]
            assert not folder.exists() or not any(folder.iterdir())
            write(folder / "MANIFEST.json", {})
            write(folder / "apps.json", {"apps": []})
            for task in ("b", "a"):
                write(folder / "tasks" / task / "workflow.json", {})
        elif command == "seed-world":
            write(Path(args[1]) / "world" / "SEED.json", {"status": "seeded_reviewed"})
        elif command == "hub-serve":
            endpoints = Path(args[1]) / "runtime" / "endpoints.json"
            write(endpoints, {})
            os.utime(endpoints, (clock(), clock()))
            return server
        return 0

    batch = queue.BatchQueue(
        "one", tmp_path / "cache", root=tmp_path, runner=runner, clock=clock, sleep=clock.sleep
    )
    monkeypatch.setattr(batch, "prepare_work", lambda folder, log: batch.dir / "work" / folder.name)
    return SimpleNamespace(batch=batch, root=tmp_path, clock=clock, calls=calls, runner=runner, server=server)


def at_step(setup, step, company="acme"):
    folder = setup.root / "companies" / company
    state = queue.read_state(folder)
    for name in queue.STEPS[: queue.STEPS.index(step)]:
        state["steps"][name].update(status="ok", attempts=1)
    queue.save_state(folder, state)
    return folder


def test_pipeline_calls_and_resume(setup):
    setup.batch.process("run", "acme")
    folder = setup.root / "companies" / "acme"
    f = str(folder)
    work = str(setup.batch.dir / "work" / "acme")
    assert setup.calls == [
        (["export-company", "run", "acme"], 300),
        (["rewrite-briefs", f], 1800),
        (["assign-apps", f], 1800),
        (
            [
                "seed-world",
                f,
                "--timeout",
                "2400",
                "--verify",
                "--host",
                "127.0.0.1",
                "--work",
                work,
                "--review-rounds",
                "2",
            ],
            18000,
        ),
        (["dedupe-names", f], 600),
        (["sync-worker-apps", f], 120),
        (["add-bulk", f], 3600),
        (["readability", f], 600),
        (["world-check", f], 600),
        (["hub-serve", f, "--host", "127.0.0.1", "--work", work], None),
        (["author-golden", f, "a"], 3600),
        (["calibrate", f, "a", "--golden"], 3600),
        (["author-golden", f, "b"], 3600),
        (["calibrate", f, "b", "--golden"], 3600),
        (["run-company", f, "a", "--backend", "fake", "--dry-run"], 1800),
        (["run-company", f, "b", "--backend", "fake", "--dry-run"], 1800),
        (["review-sheet", f], 300),
        (["agreement", f], 120),
    ]
    state = queue.read_state(folder)
    assert state["claim"] is None
    assert all(r["status"] == "ok" and r["attempts"] == 1 for r in state["steps"].values())
    assert all(r["started_at"] and r["ended_at"] for r in state["steps"].values())
    assert setup.server.stopped
    setup.batch.process("run", "acme")
    assert len(setup.calls) == 18


def test_two_drivers_cannot_run_same_company(setup):
    entered, release = threading.Event(), threading.Event()

    def runner(cmd, log, timeout):
        entered.set()
        assert release.wait(5)
        return 1

    setup.batch.runner = runner
    other = queue.BatchQueue("one", setup.root / "cache", root=setup.root, runner=runner, clock=setup.clock)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(setup.batch.process, "run", "acme")
        assert entered.wait(5)
        folder = setup.root / "companies" / "acme"
        before = queue.state_path(folder).read_text()
        pool.submit(other.process, "run", "acme").result(timeout=5)
        assert queue.state_path(folder).read_text() == before
        assert queue.read_state(folder)["claim"]["driver"] == setup.batch.driver
        assert other.driver != setup.batch.driver
        release.set()
        first.result(timeout=5)
    assert queue.read_state(folder)["steps"]["export"]["attempts"] == 1


def competing_claim(root, start, release, results):
    batch = queue.BatchQueue("driver", root / "cache", root=root, runner=lambda *args: 0)
    start.wait(5)
    with batch.claim("acme") as claim:
        results.put(claim is not None)
        if claim:
            release.wait(5)


def test_claim_atomicity_between_processes(tmp_path):
    context = multiprocessing.get_context("fork")
    start, release, results = context.Event(), context.Event(), context.Queue()
    processes = [
        context.Process(target=competing_claim, args=(tmp_path, start, release, results)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    try:
        start.set()
        assert sorted(results.get(timeout=5) for _ in processes) == [False, True]
    finally:
        release.set()
        for process in processes:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join()
    assert all(p.exitcode == 0 for p in processes)


def test_stale_takeover_and_live_lock(setup):
    folder = at_step(setup, "rewrite")
    state = queue.read_state(folder)
    state["claim"] = {"driver": "dead", "pid": 123, "heartbeat": queue.now(setup.clock() - 600)}
    state["steps"]["rewrite"].update(status="running", attempts=1)
    queue.save_state(folder, state)
    with setup.batch.claim("acme") as claim:
        assert claim is None
    setup.clock.sleep(1)
    other = queue.BatchQueue("two", setup.root / "cache", root=setup.root, clock=setup.clock)
    with setup.batch.claim("acme") as (state, _mutex):
        assert state["claim"]["driver"] == setup.batch.driver
        assert state["steps"]["rewrite"]["status"] == "failed"
        assert state["steps"]["rewrite"]["attempts"] == 1
        assert queue.ready(state["steps"]["rewrite"], setup.clock())
        setup.clock.sleep(601)
        with other.claim("acme") as claim:
            assert claim is None  # A late heartbeat cannot displace an active owner.
    with other.claim("acme") as claim:
        assert claim is not None


def test_heartbeat_refreshes_claim(setup, monkeypatch):
    assert queue.HEARTBEAT == 60
    monkeypatch.setattr(queue, "HEARTBEAT", 0.01)
    folder = setup.root / "companies" / "acme"
    with setup.batch.claim("acme"):
        setup.clock.sleep(60)
        deadline = time.monotonic() + 2
        while queue.read_state(folder)["claim"]["heartbeat"] != queue.now(setup.clock()):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert queue.read_state(folder)["claim"] is None


def test_atomic_state_replacement(setup, monkeypatch):
    folder = at_step(setup, "rewrite")
    previous = queue.read_state(folder)
    replacement = queue.read_state(folder)
    replacement["steps"]["rewrite"]["note"] = "new state"
    replace = os.replace
    seen = []

    def observe(source, destination):
        assert queue.read_state(folder) == previous
        assert json.loads(Path(source).read_text()) == replacement
        seen.append(destination)
        replace(source, destination)

    monkeypatch.setattr(queue.os, "replace", observe)
    queue.save_state(folder, replacement)
    assert seen == [queue.state_path(folder)]
    assert queue.read_state(folder) == replacement
    assert not list(folder.glob(".BATCH-*"))


def test_backoff_and_four_attempt_limit(setup):
    folder = at_step(setup, "rewrite")
    calls = []
    setup.batch.runner = lambda *args: calls.append(setup.clock()) or 1
    setup.batch.process("run", "acme")
    for delay in (120, 600, 1800):
        before = len(calls)
        setup.clock.sleep(delay - 1)
        setup.batch.process("run", "acme")
        assert len(calls) == before
        setup.clock.sleep(1)
        setup.batch.process("run", "acme")
        assert len(calls) == before + 1
    assert [calls[i + 1] - calls[i] for i in range(3)] == [120, 600, 1800]
    state = queue.read_state(folder)
    assert state["steps"]["rewrite"]["attempts"] == 4
    assert queue.finished(state)
    setup.clock.sleep(99999)
    setup.batch.process("run", "acme")
    assert len(calls) == 4


@pytest.mark.parametrize("step", ["calibrate", "controller"])
def test_review_gate_is_terminal(setup, step):
    folder = at_step(setup, step)
    write(folder / "world" / "SEED.json", {"status": "review_failed"})
    setup.batch.process("run", "acme")
    state = queue.read_state(folder)
    assert state["steps"][step]["note"] == "rc=review_not_accepted"
    assert queue.finished(state)
    setup.clock.sleep(99999)
    setup.batch.process("run", "acme")
    assert setup.calls == []
    assert queue.read_state(folder)["steps"][step]["attempts"] == 1


def test_agreement_failure_is_terminal(setup):
    folder = at_step(setup, "agreement")
    setup.batch.runner = lambda *args: 1
    setup.batch.process("run", "acme")
    row = queue.read_state(folder)["steps"]["agreement"]
    assert row["note"] == "agreement failed: rc=1"
    assert queue.terminal(row)
    assert row["attempts"] == 1


@pytest.mark.parametrize("note", ["rc=review_not_accepted", "agreement failed: rc=1"])
def test_terminal_notes_are_not_retried(note):
    row = {"status": "failed", "attempts": 1, "note": note}
    assert queue.terminal(row)
    assert not queue.ready(row, 99999)


@pytest.mark.parametrize("raise_error", [False, True])
def test_usage_limit_pauses_whole_driver_without_spending_attempts(setup, raise_error):
    folder = at_step(setup, "rewrite")
    peer = at_step(setup, "rewrite", "peer")
    calls = []

    class ModelUnavailable(Exception):
        pass

    def unavailable(cmd, log, timeout):
        calls.append(cmd)
        if raise_error:
            raise ModelUnavailable("Usage limit reached")
        with log.open("a") as stream:
            stream.write("ModelUnavailable: Usage limit reached\n")
        return 1

    setup.batch.runner = unavailable
    setup.batch.process("run", "acme")
    row = queue.read_state(folder)["steps"]["rewrite"]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert setup.batch.pause_until == setup.clock() + 600
    setup.batch.process("run", "peer")
    assert len(calls) == 1
    assert queue.read_state(peer)["steps"]["rewrite"]["status"] == "pending"
    setup.clock.sleep(599)
    assert setup.batch.paused()
    setup.clock.sleep(1)
    assert not setup.batch.paused()
    setup.batch.runner = lambda *args: 1
    setup.batch.process("run", "acme")
    row = queue.read_state(folder)["steps"]["rewrite"]
    assert row["status"] == "failed" and row["attempts"] == 1
    assert not setup.batch.paused()  # The previous log entry must not trigger another pause.


def test_active_company_stops_before_next_command_on_usage_pause(setup):
    entered, release = threading.Event(), threading.Event()
    at_step(setup, "rewrite", "peer")
    peer_calls = []

    def runner(cmd, log, timeout):
        if "peer" in str(cmd):
            peer_calls.append(cmd)
            entered.set()
            assert release.wait(5)
            return 0
        log.write_text("ModelUnavailable: usage limit")
        return 1

    setup.batch.runner = runner
    with ThreadPoolExecutor(1) as pool:
        peer = pool.submit(setup.batch.process, "run", "peer")
        assert entered.wait(5)
        setup.batch.process("run", "acme")
        release.set()
        peer.result(timeout=5)
    assert len(peer_calls) == 1
    assert queue.read_state(setup.root / "companies" / "peer")["steps"]["apps"]["status"] == "pending"


@pytest.mark.parametrize("message", ["usage limit", "ModelUnavailable: network failed"])
def test_other_logs_use_normal_retries(setup, message):
    def runner(cmd, log, timeout):
        log.write_text(message)
        return 1

    setup.batch.runner = runner
    setup.batch.process("run", "acme")
    assert not setup.batch.paused()
    assert queue.read_state(setup.root / "companies" / "acme")["steps"]["export"]["status"] == "failed"


def test_status_shows_furthest_step_and_unclaimed_companies(setup, capsys):
    setup.batch.process("run", "done")
    folder = at_step(setup, "seed", "failed")
    state = queue.read_state(folder)
    state["steps"]["seed"].update(status="failed", attempts=2, note="timeout\ntry again")
    queue.save_state(folder, state)
    write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"done": "", "failed": "", "new": ""}})
    setup.batch.status(["run"])
    assert capsys.readouterr().out.splitlines() == [
        "done: agreement ok (attempts=1)",
        "failed: seed failed (attempts=2) — timeout try again",
        "new: export pending (attempts=0)",
    ]
    setup.batch.status()
    assert len(capsys.readouterr().out.splitlines()) == 2


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("cmd", 1), OSError("missing command")])
def test_command_exceptions_release_claim_and_allow_retry(setup, failure):
    def runner(*args):
        raise failure

    setup.batch.runner = runner
    setup.batch.process("run", "acme")
    state = queue.read_state(setup.root / "companies" / "acme")
    assert state["claim"] is None
    assert state["steps"]["export"]["status"] == "failed"
    assert state["steps"]["export"]["note"] == (
        "timeout" if isinstance(failure, subprocess.TimeoutExpired) else "OSError: missing command"
    )
    assert not queue.finished(state)


def test_existing_export_seed_and_golden_are_reused(setup):
    setup.batch.process("run", "acme")
    folder = setup.root / "companies" / "acme"
    queue.save_state(folder, queue.read_state(setup.root / "unused"))
    write(folder / "tasks" / "a" / "golden.json", {})
    setup.calls.clear()
    setup.batch.process("run", "acme")
    commands = [args[0] for args, _ in setup.calls]
    assert "export-company" not in commands
    assert "seed-world" not in commands
    assert [args[2] for args, _ in setup.calls if args[0] == "author-golden"] == ["b"]
    assert queue.finished(queue.read_state(folder))


def test_seed_receipt_keeps_failed_review_for_gate(setup):
    folder = at_step(setup, "seed")

    def runner(cmd, log, timeout):
        setup.calls.append(cmd)
        if "seed-world" in cmd:
            write(folder / "world" / "SEED.json", {"status": "review_failed"})
            return 1
        return 0

    setup.batch.runner = runner
    setup.batch.process("run", "acme")
    state = queue.read_state(folder)
    assert state["steps"]["seed"]["status"] == "ok"
    assert state["steps"]["calibrate"]["note"] == "rc=review_not_accepted"
    assert not any("hub-serve" in cmd for cmd in setup.calls)


@pytest.mark.parametrize("mode", ["dead", "timeout", "usage", "stale"])
def test_server_readiness_failure_and_cleanup(setup, mode):
    folder = at_step(setup, "calibrate")
    write(folder / "world" / "SEED.json", {"status": "seeded_reviewed"})
    if mode == "stale":
        endpoints = folder / "runtime" / "endpoints.json"
        write(endpoints, {})
        os.utime(endpoints, (setup.clock() - 1, setup.clock() - 1))
    server = Server(dead=mode in ("dead", "usage"), hangs=True)

    def runner(cmd, log, timeout):
        assert "hub-serve" in cmd
        if mode == "usage":
            log.write_text("ModelUnavailable: usage limit reached")
        return server

    setup.batch.runner = runner
    setup.batch.process("run", "acme")
    row = queue.read_state(folder)["steps"]["calibrate"]
    assert server.stopped and server.killed
    if mode == "usage":
        assert row["status"] == "pending" and row["attempts"] == 0
        assert setup.batch.paused()
    else:
        assert row["note"] == "rc=serve_failed"
        assert row["attempts"] == 1


def test_usage_limit_during_golden_stops_task_loop_and_server(setup):
    original = setup.runner

    def runner(cmd, log, timeout):
        if "author-golden" in cmd:
            log.write_text("ModelUnavailable: usage limit")
            return 1
        return original(cmd, log, timeout)

    setup.batch.runner = runner
    setup.batch.process("run", "acme")
    row = queue.read_state(setup.root / "companies" / "acme")["steps"]["calibrate"]
    assert row["status"] == "pending" and row["attempts"] == 0
    assert setup.server.stopped
    assert not any(args[0] in ("calibrate", "run-company") for args, _ in setup.calls)


def test_real_runner_passes_company_lock_and_worktree_pythonpath(setup, monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert os.fstat(kwargs["pass_fds"][0])
        return SimpleNamespace(
            returncode=0, wait=lambda timeout=None: 0, send_signal=lambda sig: None, kill=lambda: None
        )

    monkeypatch.setattr(queue.subprocess, "run", run)
    monkeypatch.setattr(queue.subprocess, "Popen", lambda cmd, **kwargs: run(cmd, **kwargs))
    with setup.batch.claim("acme"):
        log = setup.batch.dir / "acme.log"
        assert setup.batch.run_command(["fake"], log, 3) == 0
        setup.batch.run_command(["fake-serve"], log, None)
    assert len(calls) == 2
    assert (
        "timeout" not in calls[0][1]
    )  # the timeout is enforced by wait(), so the CLI can be interrupted first
    assert calls[0][1]["env"]["PYTHONPATH"] == str(setup.root / "src")
    assert calls[0][1]["cwd"] == setup.root
    assert "timeout" not in calls[1][1]


def test_prepare_work_uses_doubles(setup, monkeypatch):
    # prepare_work was copied from the old batch_companies.py; that driver has since been rewritten
    # as plain functions, so the queue's copy is tested on its own behaviour here.
    folder = setup.root / "companies" / "acme"
    write(folder / "apps.json", {"apps": [{"app_id": "mail"}]})
    source = setup.batch.work / "mail"
    source.mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "cp":
            Path(cmd[-1]).mkdir()
        if "stdout" in kwargs:
            kwargs["stdout"].close()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(queue.subprocess, "run", fake_run)
    with setup.batch.claim("acme"):
        work = queue.BatchQueue.prepare_work(setup.batch, folder, setup.batch.dir / "acme.log")
        assert queue.BatchQueue.prepare_work(setup.batch, folder, setup.batch.dir / "acme.log") == work
    assert setup.calls[0][0][:2] == ["hub-smoke", "mail"]
    assert calls == [
        ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
        ["cp", "-al", str(source), str(work / "mail")],
        ["rm", "-rf", str(work / "mail" / ".mock-states")],
        ["rm", "-rf", str(work / "mail" / ".mock-files")],
        # A hardlinked copy must not inherit the warm cache's claim on itself, or HubProcess
        # refuses this private build as already served by the process holding the cache.
        ["rm", "-rf", str(work / "mail" / ".hub-serving")],
    ]


def test_driver_deduplicates_runs_and_respects_parallel_limit(setup):
    for run in ("first", "second"):
        write(setup.root / "runs" / run / "dataset.json", {"companies": {c: "" for c in ("a", "b", "c")}})
    active = maximum = 0
    mutex = threading.Lock()
    original = setup.runner

    def runner(cmd, log, timeout):
        nonlocal active, maximum
        with mutex:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.002)
            return original(cmd, log, timeout)
        finally:
            with mutex:
                active -= 1

    setup.batch.runner = runner
    setup.batch.drive(["first", "second"], parallel=2)
    assert maximum == 2
    assert len([c for c, _ in setup.calls if c[0] == "export-company"]) == 3
    assert all(queue.finished(queue.read_state(setup.root / "companies" / c)) for c in ("a", "b", "c"))


def test_driver_waits_through_backoff_until_terminal(setup):
    write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"acme": ""}})
    folder = at_step(setup, "rewrite")
    calls = []
    setup.batch.runner = lambda *args: calls.append(setup.clock()) or 1
    setup.batch.drive(["run"], parallel=1)
    assert len(calls) == 4
    assert all(calls[i + 1] - calls[i] >= delay for i, delay in enumerate(queue.BACKOFF))
    assert queue.finished(queue.read_state(folder))


def test_once_exits_after_one_attempt_and_usage_pause(setup):
    write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"acme": "", "peer": ""}})
    calls = []

    def runner(cmd, log, timeout):
        calls.append(cmd)
        log.write_text("ModelUnavailable: usage limit")
        return 1

    setup.batch.runner = runner
    setup.batch.drive(["run"], parallel=1, once=True)
    assert len(calls) == 1
    assert setup.batch.paused()
    assert not (setup.root / "companies" / "peer").exists()


def test_once_leaves_backoff_pending(setup):
    write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"acme": ""}})
    setup.batch.runner = lambda *args: 1
    setup.batch.drive(["run"], once=True)
    setup.batch.drive(["run"], once=True)
    assert queue.read_state(setup.root / "companies" / "acme")["steps"]["export"]["attempts"] == 1


def test_driver_waits_for_foreign_claim_then_finishes(setup):
    write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"acme": ""}})
    folder = setup.root / "companies" / "acme"
    state = queue.read_state(folder)
    state["claim"] = {"driver": "gone", "pid": 123, "heartbeat": queue.now(setup.clock())}
    queue.save_state(folder, state)
    started = setup.clock()
    setup.batch.drive(["run"], parallel=1)
    assert setup.clock() - started > 600
    assert queue.finished(queue.read_state(folder))


def test_driver_watches_empty_run_until_company_arrives(setup):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        write(setup.root / "runs" / "run" / "dataset.json", {"companies": {"acme": ""}})
        setup.clock.sleep(seconds)

    setup.batch.sleep = sleep
    setup.batch.drive(["run"], parallel=1)
    assert sleeps
    assert queue.finished(queue.read_state(setup.root / "companies" / "acme"))


def test_empty_finished_run_and_empty_once_exit(setup):
    setup.batch.drive(["absent"], once=True)
    write(setup.root / "runs" / "run" / "run.json", {"status": "completed"})
    setup.batch.drive(["run"])
    assert not setup.calls


def test_skipped_steps_are_finished(setup):
    folder = at_step(setup, "agreement")
    state = queue.read_state(folder)
    state["steps"]["agreement"]["status"] = "skipped"
    queue.save_state(folder, state)
    setup.batch.process("run", "acme")
    assert not setup.calls
    assert queue.finished(state)


@pytest.mark.parametrize("argv", [["driver"], ["driver", "--runs", "run", "--parallel", "0"]])
def test_cli_rejects_invalid_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        queue.main(argv)
    assert exc.value.code == 2


def test_status_cli_without_runs(setup, monkeypatch, capsys):
    at_step(setup, "rewrite")
    monkeypatch.setattr(queue, "BatchQueue", lambda *args: setup.batch)
    queue.main(["status"])
    assert capsys.readouterr().out == "acme: export ok (attempts=1)\n"


def test_run_cli_passes_options(setup, monkeypatch):
    seen = []
    monkeypatch.setattr(queue, "BatchQueue", lambda *args: setup.batch)
    monkeypatch.setattr(setup.batch, "drive", lambda *args: seen.append(args))
    queue.main(["driver", "--runs", "one", "two", "--parallel", "3", "--once"])
    assert seen == [(["one", "two"], 3, True)]
