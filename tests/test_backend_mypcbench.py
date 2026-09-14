"""Transport, inference, and controller wiring doubles; no VMs or model calls."""

import asyncio
import base64
import copy
import json
import shlex
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from company_envs import models as model_backend
from company_envs.storage import read, write
from company_envs.world import controller, hub_vm
from company_envs.world.backends import mypcbench as backend
from company_envs.world.harness import Action, Budget, Episode, FakeBackend, Observation, Screen, WorkerAgent


def response(stdout=b"ok\n", stderr=b"", code=0):
    return {
        "stdout": base64.b64encode(stdout).decode(),
        "stderr": base64.b64encode(stderr).decode(),
        "exit_code": code,
        "reason": "complete",
        "bytes": {"stdout": len(stdout), "stderr": len(stderr)},
        "truncated": {"stdout": False, "stderr": False},
    }


class Transport:
    def __init__(self):
        self.calls = []

    async def run(self, command, *, deadline, limit=65536):
        self.calls.append((command, deadline, limit))
        return response(FakeBackend.PNG if "screen.png" in command else b"ok\n")


def record(root, worker="boss"):
    path = root / "runtime/vms" / worker / "vm.json"
    write(
        path,
        {
            "worker": worker,
            "status": "running",
            "dry_run": False,
            "vm": {"ssh_port": 22001, "process": {"pid": 9999999}},
        },
    )
    return path


def test_actions_capture_and_trace(tmp_path):
    transport = Transport()
    vm = backend.MyPCBenchBackend(record(tmp_path), transport=transport)

    async def run():
        deadline = asyncio.get_running_loop().time() + 5
        text = "it's $(touch /tmp/NEVER) `echo no`; --help\nhello"
        for action in (
            Action("click", {"x": 12, "y": 30}),
            Action("type", {"text": text}),
            Action("key", {"key": "ctrl+l"}),
            Action("bash", {"command": "echo ok"}),
        ):
            assert (await vm.execute(action, deadline=deadline))["stdout"] == "ok\n"
        await vm.open_url("http://10.0.2.2:9000/?sid=x&foo=y", deadline=deadline)
        assert (await vm.observe(deadline=deadline)).png == FakeBackend.PNG
        assert base64.b64decode(shlex.split(transport.calls[1][0])[-1]).decode() == text
        assert transport.calls[3][0] == backend.DESKTOP + "echo ok"
        assert all(call[1] == deadline for call in transport.calls)

    asyncio.run(run())
    trace = vm.directory / "trace"
    assert len(list((trace / "transport").glob("*.png"))) == 1
    events = [json.loads(line) for line in (trace / "transport.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["action", "result"] * 5 + ["capture", "screen"]


def test_harness_owns_ordered_screens_and_budgeted_open_url(tmp_path):
    vm = backend.MyPCBenchBackend(record(tmp_path), transport=Transport())
    result = asyncio.run(
        Episode(
            [
                WorkerAgent(
                    "boss",
                    "Manager",
                    lambda obs: Action("open_url", {"url": "https://example.test"}),
                    vm,
                    Budget(1, 5),
                )
            ],
            boss_id="boss",
            brief="Brief",
            runtime=tmp_path / "runtime",
            seconds=5,
        ).run()
    )
    assert result["reason"] == "budget_exhausted"
    trace = tmp_path / "runtime/vms/boss/trace"
    assert len(list(trace.glob("screen-*.png"))) == 2
    events = [json.loads(line) for line in (trace / "events.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["event"] == "action"]) == 1


@pytest.mark.parametrize("change", [{"dry_run": True}, {"status": "stopped"}, {"worker": "other"}])
def test_stale_or_wrong_record_refused(tmp_path, change):
    path = record(tmp_path)
    write(path, read(path) | change)
    with pytest.raises(ValueError):
        backend.MyPCBenchBackend(path, transport=Transport())


def test_default_transport_checks_process_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_vm, "_process_identity", lambda pid: None)
    with pytest.raises(ValueError, match="stale"):
        backend.MyPCBenchBackend(record(tmp_path))


def test_expired_actions_never_reach_transport(tmp_path):
    transport = Transport()
    vm = backend.MyPCBenchBackend(record(tmp_path), transport=transport)

    async def run():
        with pytest.raises(TimeoutError):
            await vm.execute(Action("bash", {"command": "echo forbidden"}), deadline=0)

    asyncio.run(run())
    assert not transport.calls


def test_bad_capture_fails_closed(tmp_path):
    class Bad(Transport):
        async def run(self, *args, **kwargs):
            return response(b"not PNG")

    vm = backend.MyPCBenchBackend(record(tmp_path), transport=Bad())

    async def run():
        with pytest.raises(RuntimeError, match="screenshot"):
            await vm.observe(deadline=asyncio.get_running_loop().time() + 2)

    asyncio.run(run())


def test_ssh_routing_supervisor_and_cancellation(tmp_path, monkeypatch):
    calls = []
    killed = []

    class Input:
        def close(self):
            if process.closed:
                return
            process.closed = True
            process.stdout.feed_data(json.dumps(response()).encode())
            process.stdout.feed_eof()
            process.stderr.feed_eof()

    class Process:
        pid = 3456789
        returncode = 0
        closed = False

        async def wait(self):
            return 0

    process = Process()

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        process.stdin = Input()
        process.stdout, process.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        return process

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(backend.os, "killpg", lambda pid, sig: killed.append(pid))

    async def run():
        transport = backend.SSHTransport(read(record(tmp_path)), tmp_path)
        task = asyncio.create_task(transport.run("echo ok", deadline=asyncio.get_running_loop().time() + 5))
        while not calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    argv, kwargs = calls[0]
    assert argv[:3] == ("sshpass", "-e", "ssh")
    assert "ga@127.0.0.1" in argv and "22001" in argv
    remote = shlex.split(argv[-1])
    assert remote[:3] == ["python3", "-u", "-c"]
    assert remote[3] == backend.REMOTE_RUNNER
    assert json.loads(remote[4])[0] == "echo ok"
    assert kwargs["start_new_session"] and kwargs["env"]["SSHPASS"] == "password123"
    assert process.closed and killed == [process.pid]


# Exercise the guest supervisor itself on an isolated local child, not a VM or
# backend fallback. This checks cleanup semantics beyond mocked SSH calls.
@pytest.mark.parametrize("mode", ["complete", "deadline", "disconnect", "background"])
def test_remote_supervisor_bounds_output_and_drains(mode, tmp_path):
    script = "printf 123456789"
    if mode != "complete":
        script = f"sleep 0.3; touch {shlex.quote(str(tmp_path / 'late'))}"
    if mode == "background":
        script = f"({script}) & echo ok"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            backend.REMOTE_RUNNER,
            json.dumps([script, 0.05 if mode == "deadline" else 2, 4]),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if mode == "disconnect":
            proc.stdin.close()
        proc.wait(timeout=3)
        result = json.loads(proc.stdout.read())
        if mode == "complete":
            assert base64.b64decode(result["stdout"]) == b"1234"
            assert result["bytes"]["stdout"] == 9
            assert result["truncated"]["stdout"] is True
        else:
            assert (
                result["reason"]
                == {"deadline": "deadline", "disconnect": "cancelled", "background": "complete"}[mode]
            )
            time.sleep(0.35)
            assert not (tmp_path / "late").exists()
    finally:
        proc.kill() if proc.poll() is None else None
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def observation(**kwargs):
    return Observation(
        "boss",
        "Manager",
        (("boss", "Manager"), ("peer", "Support")),
        Screen(FakeBackend.PNG),
        (),
        "Only boss sees this",
        0,
        5,
        3,
        None,
        **kwargs,
    )


CONFIG = {
    "models": {"expand": ["codex/gpt-test", "codex/gpt-fallback"], "reasoning": "low"},
    "generation": {"completion_timeout_seconds": 99},
}


class Model:
    def __init__(self):
        self.calls = []

    def call(self, job, prompt, response_type, *, images):
        self.calls.append((job, json.loads(prompt.split("\n")[-1]), images[0].read_bytes()))
        return {"name": "bash", "command": "echo ok"}, {"job": job, "model": "double"}


def test_a_worker_is_told_what_its_colleagues_are_responsible_for(tmp_path):
    """Deciding who to ask needs the roster's duties, not just its job titles.

    The MACU manager reads them from company.json; without this our own policy saw only
    id-to-title pairs, so the two harnesses were not being asked to plan from the same facts.
    """
    write(
        tmp_path / "company.json",
        {
            "name": "Harborlight Hospice Services",
            "workers": [
                {"id": "boss", "title": "Manager", "responsibility": "Coordinate the review."},
                {"id": "peer", "title": "Support", "responsibility": "Answer families."},
                {"id": "absent", "title": "Driver", "responsibility": "Not on this task."},
            ],
        },
    )
    model = Model()
    policy = backend.WorkerPolicy(CONFIG, tmp_path / "runtime" / "vms" / "boss" / "policy", models=model)
    asyncio.run(policy(observation()))
    duties = model.calls[0][1]["roster_duties"]
    assert duties == {"boss": "Coordinate the review.", "peer": "Answer families."}
    assert "absent" not in duties  # only the roster on this task, not everyone at the company


def test_no_company_file_leaves_the_duties_out(tmp_path):
    model = Model()
    policy = backend.WorkerPolicy(CONFIG, tmp_path / "boss", models=model)
    asyncio.run(policy(observation()))
    assert model.calls[0][1]["roster_duties"] is None


def test_policy_image_brief_role_history_bash_and_reservations(tmp_path):
    model = Model()
    reservations = []
    policy = backend.WorkerPolicy(
        CONFIG, tmp_path / "boss", models=model, reserve=lambda: reservations.append(1)
    )
    frozen = copy.deepcopy(CONFIG)

    async def run():
        assert (await policy(observation())).name == "bash"
        await policy(replace(observation(), brief=None, last_result={"stdout": "ok"}, remaining_actions=2))
        peer = backend.WorkerPolicy(CONFIG, tmp_path / "peer", role_note="Help customers", models=model)
        await peer(replace(observation(), worker_id="peer", role="Support", brief=None))

    asyncio.run(run())
    assert reservations == [1, 1]
    assert CONFIG == frozen
    assert policy.config["models"]["worker_policy"] == ["codex/gpt-test"]
    job, packet, png = model.calls[1]
    assert job == "worker_policy" and png == FakeBackend.PNG
    assert packet["public_brief"] == "Only boss sees this"
    assert packet["last_bash_result"] == {"stdout": "ok"}
    assert packet["remaining_actions"] == 2
    assert packet["history"][1]["action"]["name"] == "bash"
    assert model.calls[2][1]["public_brief"] is None
    assert model.calls[2][1]["role_note"] == "Help customers"
    assert "Only boss sees this" not in json.dumps(model.calls[2][1])


def test_policy_budget_fails_closed_and_a_refused_action_is_fed_back_not_fatal(tmp_path):
    answers = iter(
        [
            {"name": "click", "x": -1, "y": 3},
            {"name": "open_url", "url": "TABLEAU_ORIGIN/views/plant"},
            {"name": "bash", "command": "echo ok"},
        ]
    )

    class Invalid(Model):
        def call(self, job, prompt, response_type, *, images):
            self.calls.append(json.loads(prompt.split("\n")[-1]))
            return next(answers), {}

    model = Invalid()
    policy = backend.WorkerPolicy(CONFIG, tmp_path, models=model)

    async def run():
        with pytest.raises(TimeoutError):
            await policy(replace(observation(), remaining_actions=0))
        # The harness refuses the action; the worker spends one action on a screenshot and the
        # next prompt's history says what was refused and why, instead of the episode dying.
        assert (await policy(observation())).name == "screenshot"
        assert (await policy(replace(observation(), brief=None))).name == "screenshot"
        assert (await policy(replace(observation(), brief=None))).name == "bash"

    asyncio.run(run())
    rejected = [h for h in model.calls[-1]["history"] if "rejected_action" in h]
    assert [r["rejected_action"]["name"] for r in rejected] == ["click", "open_url"]
    assert "http, https or file URL" in rejected[1]["reason"]


def test_child_calls_models_with_image_and_restores_globals(tmp_path, monkeypatch):
    calls = []
    original = model_backend.command, model_backend.execute

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        write(kwargs["cwd"] / "answer.json", {"name": "done", "summary": "Finished"})
        (kwargs["cwd"] / "stdout.jsonl").write_text('{"type":"turn.completed","usage":{}}\n')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(backend.subprocess, "run", run)
    request = tmp_path / "request.json"
    config = copy.deepcopy(CONFIG)
    config["models"]["worker_policy"] = ["codex/gpt-test"]
    write(
        request,
        {"config": config, "prompt": "Only the public context", "image": str(tmp_path / "screen.png")},
    )
    backend._call_in_child(request)
    result = read(tmp_path / "response.json")
    assert result["receipt"]["job"] == "worker_policy"
    assert result["data"]["name"] == "done"
    cmd, options = calls[0]
    assert cmd[-3:] == ["--image", str(tmp_path / "screen.png"), "-"]
    assert options["timeout"] == 99
    assert "start_new_session" not in options  # Parent owns the inference group.
    assert (model_backend.command, model_backend.execute) == original


def test_model_process_cancel_kills_owned_group(tmp_path, monkeypatch):
    calls, killed = [], []

    class Process:
        pid = 4567890
        stopped = False

        async def wait(self):
            while not self.stopped:
                await asyncio.sleep(0.001)
            return 0

    process = Process()

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    def kill(pid, sig):
        killed.append(pid)
        process.stopped = True

    monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(backend.os, "killpg", kill)

    async def run():
        task = asyncio.create_task(
            backend._model_process(
                CONFIG,
                "brief",
                tmp_path / "screen.png",
                tmp_path,
                asyncio.get_running_loop().time() + 5,
            )
        )
        while not calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert killed == [process.pid]
    assert calls[0][1]["start_new_session"] is True


def setup_context(tmp_path):
    ctx = controller.Context(
        tmp_path,
        tmp_path / "company",
        {
            "options": {
                "backend": "mypcbench",
                "dry_run": False,
                "base_image": "/base.qcow2",
                "browser_dir": "/browser",
                "host_ip": "10.0.2.2",
            },
            "task_id": "task",
            "model_calls_used": 0,
            "budgets": {"model_calls": 10},
        },
        "episode",
        time.monotonic() + 10,
    )
    (tmp_path / "config.toml").write_text('[models]\nexpand=["codex/gpt-test"]\nreasoning="low"\n')
    write(
        ctx.work / "company.json",
        {"workers": [{"id": "boss", "title": "Manager"}, {"id": "peer", "title": "Support"}]},
    )
    write(ctx.work / "tasks/task/workflow.json", {"manager_id": "boss", "worker_ids": ["peer"]})
    write(ctx.work / "tasks/task/assignment.json", {"brief": "Boss-only task", "private": "DO NOT SHARE"})
    return ctx


def test_controller_real_launch_episode_stop_then_grade_doubles(tmp_path, monkeypatch):
    ctx = setup_context(tmp_path)
    events, models = [], []
    monkeypatch.setattr(controller, "_live", lambda ctx: None)

    def launch(folder, task_id, **kwargs):
        assert folder == ctx.work and task_id == "task"
        assert kwargs["dry_run"] is False and kwargs["workers"] == ["boss", "peer"]
        events.append("launch")
        for worker in kwargs["workers"]:
            record(folder, worker)
        return {"status": "running"}

    def stop(folder):
        events.append("stop")
        return {"status": "stopped"}

    real_backend, real_policy = backend.MyPCBenchBackend, backend.WorkerPolicy

    class DoneModel(Model):
        def call(self, *args, **kwargs):
            super().call(*args, **kwargs)
            events.append("model")
            return {"name": "done"}, {"job": "worker_policy"}

    def policy(*args, **kwargs):
        model = DoneModel()
        models.append(model)
        return real_policy(*args, **kwargs, models=model)

    monkeypatch.setattr(hub_vm, "launch_company", launch)
    monkeypatch.setattr(hub_vm, "stop_company", stop)
    monkeypatch.setattr(backend, "MyPCBenchBackend", lambda path: real_backend(path, transport=Transport()))
    monkeypatch.setattr(backend, "WorkerPolicy", policy)
    steps = controller.DefaultSteps()
    steps.launch(ctx)
    result = steps.episode(ctx)
    assert result["simulation_only"] is False and result["result"]["reason"] == "all_done"
    assert ctx.state["model_calls_used"] == 2
    assert events == ["launch", "model", "model", "stop"]
    assert models[0].calls[0][1]["public_brief"] == "Boss-only task"
    assert models[1].calls[0][1]["public_brief"] is None
    from company_envs.world import grader

    write(ctx.work / "runtime/endpoints.json", {"apps": {"zendesk": {"harness_url": "http://localhost:9"}}})
    write(ctx.work / "tasks/task/grader.json", {})
    write(ctx.work / "runtime/grades/task/calibration.json", {"accepted": True, "scope": "all_checks"})
    monkeypatch.setattr(grader, "calibrate", lambda *args, **kwargs: events.append("calibrate"))
    monkeypatch.setattr(controller, "_MeteredModels", lambda context, **kw: "grader model double")

    def grade(*args, **kwargs):
        assert events[-1] == "stop"  # calibration happens in its own step before the teacher
        events.append("grade")
        return {"passed": True}

    monkeypatch.setattr(grader, "grade", grade)
    assert steps.grade(ctx) == {"passed": True}
    assert events == ["launch", "model", "model", "stop", "grade"]


@pytest.mark.parametrize("missing", ["base_image", "browser_dir", "host_ip", "dry_run", "budget"])
def test_controller_refuses_invalid_real_launch_before_effects(tmp_path, monkeypatch, missing):
    ctx = setup_context(tmp_path)
    if missing == "budget":
        ctx.state["budgets"]["model_calls"] = 0
    elif missing == "dry_run":
        ctx.state["options"][missing] = True
    else:
        ctx.state["options"][missing] = None
    monkeypatch.setattr(controller, "_live", lambda ctx: pytest.fail("no live effects before validation"))
    with pytest.raises(ValueError, match="mypcbench requires"):
        controller.DefaultSteps().launch(ctx)


def test_controller_stops_vms_when_policy_setup_fails(tmp_path, monkeypatch):
    ctx = setup_context(tmp_path)
    stopped = []
    monkeypatch.setattr(controller, "_live", lambda ctx: None)
    monkeypatch.setattr(hub_vm, "stop_company", lambda folder: stopped.append(folder))
    with pytest.raises(FileNotFoundError):
        controller.DefaultSteps().episode(ctx)
    assert stopped == [ctx.work]


def test_controller_stops_vms_when_service_check_fails(tmp_path, monkeypatch):
    ctx = setup_context(tmp_path)
    stopped = []

    def failed(context):
        raise RuntimeError("service is gone")

    monkeypatch.setattr(controller, "_live", failed)
    monkeypatch.setattr(hub_vm, "stop_company", lambda folder: stopped.append(folder))
    with pytest.raises(RuntimeError, match="service is gone"):
        controller.DefaultSteps().episode(ctx)
    assert stopped == [ctx.work]


def test_insider_reference_is_withheld_from_policy_request_logs(tmp_path):
    import asyncio
    import json

    from company_envs.world.backends import mypcbench

    async def run():
        directory = tmp_path / "call"
        directory.mkdir()
        image = tmp_path / "screen.png"
        image.write_bytes(b"png")
        logged = (
            mypcbench.INSTRUCTIONS
            + "\n"
            + json.dumps({"role_note": "[insider reference withheld from logs]"})
        )
        real = (
            mypcbench.INSTRUCTIONS + "\n" + json.dumps({"role_note": "INSIDER REFERENCE: step 1 write doc-x"})
        )

        async def fake_exec(*args, **kwargs):
            raise RuntimeError("stop before spawning a model process")

        original = mypcbench.asyncio.create_subprocess_exec
        mypcbench.asyncio.create_subprocess_exec = fake_exec
        try:
            with pytest.raises(RuntimeError):
                await mypcbench._model_process({}, real, image, directory, 10**12, logged=logged)
        finally:
            mypcbench.asyncio.create_subprocess_exec = original
        request = json.loads((directory / "request.json").read_text())
        assert "INSIDER REFERENCE" not in request["prompt"] and "withheld" in request["prompt"]
        # The model still receives the real prompt through the private file.
        assert (directory / "prompt.private").read_text() == real

    asyncio.run(run())


def test_transient_ssh_failures_are_retried_before_becoming_environment_errors(monkeypatch):
    import asyncio

    from company_envs.world.backends import mypcbench

    cls = next(
        c
        for c in vars(mypcbench).values()
        if isinstance(c, type) and hasattr(c, "_run_once") and hasattr(c, "run")
    )
    vm = object.__new__(cls)
    attempts = []

    async def flaky(self, command, *, deadline, limit=65536):
        attempts.append(command)
        if len(attempts) < 3:
            raise RuntimeError(
                "VM SSH failed (255): ssh: connect to host 127.0.0.1 port 2222: Connection timed out"
                if len(attempts) == 1
                else "VM SSH failed (255): Connection timed out during banner exchange"
            )
        return {"exit_code": 0, "stdout": "ok", "reason": "done"}

    monkeypatch.setattr(cls, "_run_once", flaky)

    async def nosleep(_seconds):
        return None

    monkeypatch.setattr(mypcbench.asyncio, "sleep", nosleep)

    async def go():
        return await vm.run("true", deadline=asyncio.get_running_loop().time() + 10**6)

    result = asyncio.run(go())
    assert result["stdout"] == "ok" and len(attempts) == 3

    attempts.clear()

    async def hard(self, command, *, deadline, limit=65536):
        attempts.append(command)
        raise RuntimeError("VM SSH failed (1): Permission denied")

    monkeypatch.setattr(cls, "_run_once", hard)
    with pytest.raises(RuntimeError, match="Permission denied"):
        asyncio.run(go())
    assert len(attempts) == 1  # a non-transient failure is not retried


@pytest.mark.parametrize(
    "message",
    [
        "VM SSH failed (255): Timeout, server 127.0.0.1 not responding.",
        "VM SSH failed (255): Connection reset by peer",
        "VM SSH failed (255): Connection closed by remote host",
    ],
)
def test_unknown_post_dispatch_disconnects_are_not_safe_to_retry(message):
    assert not backend._transient_ssh(message)
