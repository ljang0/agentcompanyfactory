"""Controller contracts, using doubles and an offline default-wiring dry run."""

import copy
import inspect
import shutil
import signal
import time

import pytest

from company_envs.storage import read, write
from company_envs.world import controller
from company_envs.world.controller import (
    DEFAULT_BUDGETS,
    STEP_NAMES,
    ControllerError,
    DefaultSteps,
    FakeSteps,
    run_company,
)

# What a step the teacher's verdict cancelled records. It is ``unmeasured`` and not a failure:
# no VM booted, so nothing was learned about the workers either way -- and not a pass, which is
# the defect it is here for (``--from episode:own`` was accepted past a launch that never ran).
BLOCKED_BY_TEACHER = {
    # The shared shape from company_envs.receipt: unmeasured, never a pass, and naming what it was
    # waiting for. ``reason`` stays the bare token because BLOCKING_SKIPS matches on it.
    "outcome": "unmeasured",
    "ok": None,
    "reason": "teacher_did_not_pass",
    "missing": ["a teacher pass, which a launch and a worker episode are built on"],
    "skipped": True,
}


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "companies" / "demo"


def run(folder, steps=None, **kwargs):
    return run_company(
        folder.parents[1],
        folder,
        "task",
        budgets=kwargs.pop("budgets", DEFAULT_BUDGETS),
        steps=steps if steps is not None else FakeSteps(),
        **kwargs,
    )


def test_full_fake_run_and_completed_resume(folder):
    steps = FakeSteps()
    report = run(folder, steps)
    assert report == read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert report["status"] == "done"
    assert steps.calls == list(STEP_NAMES)
    for step in STEP_NAMES:
        row = report["steps"][step]
        assert row["status"] == "done"
        assert row["started"] and row["finished"] and row["error"] is None
        assert row["artifacts"]
        for relative in row["artifacts"].values():
            assert (folder / "runtime/tasks/task" / relative).is_file()
    resumed = FakeSteps()
    assert run(folder, resumed) == report
    assert resumed.calls == []
    assert {p.parts[0] for p in (p.relative_to(folder) for p in folder.rglob("*"))} == {"runtime"}


def test_crash_after_serve_resumes_at_launch_with_fresh_steps(folder):
    first = FakeSteps(crash_before="launch")
    with pytest.raises(KeyboardInterrupt, match="crash before launch"):
        run(folder, first)
    before = read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert before["steps"]["serve"]["status"] == "done"
    resumed = FakeSteps()
    report = run(folder, resumed)
    assert resumed.calls == [
        "launch",
        "episode:own",
        "grade:own",
        "teardown",
    ]  # calibrate and teacher ran before launch
    assert report["steps"]["load"] == before["steps"]["load"]
    assert report["sid"] == before["sid"]


def test_ungraceful_running_checkpoint_is_retried(folder):
    run(folder)
    path = folder / "runtime/tasks/task/CONTROLLER.json"
    state = read(path)
    for step in STEP_NAMES[3:]:
        state["steps"][step] = controller._pending()
    state["steps"]["launch"].update(status="running", started="interrupted")
    write(path, state)
    steps = FakeSteps()
    assert run(folder, steps)["status"] == "done"
    assert steps.calls == list(STEP_NAMES[3:])


@pytest.mark.parametrize("failure", ["exception", "false"])
def test_grade_failure_is_fail_closed_and_keeps_evidence(folder, failure):
    class FailingGrade(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            if step == "grade:own":
                if failure == "exception":
                    raise RuntimeError("grader unavailable")
                return result | {"passed": False}
            return result

    steps = FailingGrade()
    if failure == "false":
        # An episode that did not pass is a measurement, not a failed step: the teacher step
        # classifies the task next and the run completes.
        assert run(folder, steps)["status"] == "done"
        report = read(folder / "runtime/tasks/task/CONTROLLER.json")
        assert report["steps"]["grade:own"]["status"] == "done"
        assert report["steps"]["grade:own"]["result"]["passed"] is False
        assert steps.calls[-2:] == ["grade:own", "teardown"]
        assert report["policy_runs"]["own"]["passed"] is False
        assert report["policy_runs"]["own"]["environment_fault"] is False
        return
    with pytest.raises(ControllerError, match="grade:own:") as error:
        run(folder, steps)
    assert error.value.step == "grade:own"
    report = read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert report["status"] == "failed" and report["responsible_step"] == "grade:own"
    row = report["steps"]["grade:own"]
    assert row["status"] == "failed" and row["finished"] and row["error"] and row["artifacts"]
    assert report["steps"]["teardown"]["status"] == "pending"
    assert "teardown" not in steps.calls
    retry = FakeSteps()
    assert run(folder, retry)["status"] == "done"
    assert retry.calls == ["grade:own", "teardown"]


def test_from_episode_only_invalidates_suffix_and_archives_receipts(folder):
    before = run(folder)
    steps = FakeSteps()
    after = run(folder, steps, from_step="episode")  # plain name: the first configured policy
    assert steps.calls == ["episode:own", "grade:own", "teardown"]
    for step in STEP_NAMES[:4]:
        assert before["steps"][step] == after["steps"][step]
    assert before["attempt"] != after["attempt"]
    assert before["sid"] == after["sid"]
    assert any(row.get("step") == "episode:own" for row in after["history"])


def test_reset_new_sid_restores_initial_and_relaunches(folder):
    before = run(folder)
    app_path = folder / "runtime/tasks/task/controller/fake-apps.json"
    write(app_path, read(app_path) | {"current": {"records": ["dirty"]}})
    steps = FakeSteps()
    reset = run(folder, steps, reset=True)
    assert reset["status"] == "ready"
    assert reset["sid"] != before["sid"]
    assert steps.calls == ["reset", "launch"]
    apps = read(app_path)
    assert apps["sid"] == reset["sid"]
    assert apps["current"] == apps["initial"] == {"records": []}
    assert read(folder / "runtime/tasks/task/controller/fake-launch.json")["sid"] == reset["sid"]
    assert reset["steps"]["load"] == before["steps"]["load"]
    assert reset["steps"]["launch"]["status"] == "done"
    resumed = FakeSteps()
    assert run(folder, resumed)["status"] == "done"
    assert resumed.calls == ["calibrate", "teacher", "episode:own", "grade:own", "teardown"]


def test_interrupted_reset_reserves_only_one_new_sid(folder):
    before = run(folder)
    with pytest.raises(ControllerError, match="reset:"):
        run(folder, FakeSteps(fail="reset"), reset=True)
    interrupted = read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert interrupted["sid"] != before["sid"]
    assert interrupted["reset_pending"]
    after = run(folder)
    assert after["sid"] == interrupted["sid"]
    assert after["status"] == "ready" and not after["reset_pending"]


def test_model_reservations_survive_failure_and_forced_restart(folder):
    calls = []

    class Models(FakeSteps):
        def run(self, step, ctx):
            if step == "episode:own":
                ctx.model_call(calls.append, "model")
                raise RuntimeError("interrupted inference")
            return super().run(step, ctx)

    budgets = copy.deepcopy(DEFAULT_BUDGETS)
    budgets["model_calls"] = 1
    for kwargs in ({}, {}, {"from_step": "episode"}):
        with pytest.raises(ControllerError, match="episode:"):
            run(folder, Models(), budgets=budgets, **kwargs)
    assert calls == ["model"]
    assert read(folder / "runtime/tasks/task/CONTROLLER.json")["model_calls_used"] == 1


def test_deadline_interrupts_step_and_restores_alarm(folder):
    class Slow(FakeSteps):
        def run(self, step, ctx):
            if step == "check":
                time.sleep(2)
            return super().run(step, ctx)

    budgets = copy.deepcopy(DEFAULT_BUDGETS)
    budgets["seconds"]["check"] = 0.02
    alarm = signal.getsignal(signal.SIGALRM)
    with pytest.raises(ControllerError, match="check: TimeoutError"):
        run(folder, Slow(), budgets=budgets)
    assert signal.getsignal(signal.SIGALRM) == alarm
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    assert read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["serve"]["status"] == "pending"


@pytest.mark.parametrize("kwargs", [{"backend": "mypcbench"}, {"dry_run": False}])
def test_changed_resume_options_rejected(folder, kwargs):
    before = run(folder)
    with pytest.raises(ValueError, match="same task, options and budgets"):
        run(folder, **kwargs)
    assert read(folder / "runtime/tasks/task/CONTROLLER.json") == before


def test_cannot_skip_failed_prerequisite(folder):
    with pytest.raises(ControllerError):
        run(folder, FakeSteps(fail="check"))
    with pytest.raises(ValueError, match="incomplete prerequisite"):
        run(folder, from_step="episode")


def test_symlink_runtime_refused(folder, tmp_path):
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (folder / "runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        run(folder)
    assert list(outside.iterdir()) == []


@pytest.fixture
def seeded(folder):
    write(folder / "MANIFEST.json", {"company_id": "demo", "stages": {}})
    write(folder / "company.json", {"workers": [{"id": w, "title": w} for w in ("boss", "peer")]})
    write(
        folder / "apps.json",
        {
            "workers": ["boss", "peer"],
            "apps": [
                {
                    "app_id": "demo_mock",
                    "hub_seedable": True,
                    "state_file": "world/demo_mock.state.json",
                    "top_level_keys": ["records"],
                    "identity_key": None,
                }
            ],
        },
    )
    write(folder / "tasks/task/workflow.json", {"worker_ids": ["boss", "peer"], "manager_id": "boss"})
    write(folder / "tasks/task/assignment.json", {"brief": "Resolve the order."})
    write(folder / "world/demo_mock.state.json", {"records": []})
    write(folder / "world/world.json", {})
    write(folder / "world/identities.json", {})
    write(folder / "world/SEED.json", {"reference_date": "2026-09-07"})
    write(folder / "world/worker_apps.json", {w: ["demo_mock"] for w in ("boss", "peer")})
    for worker in ("boss", "peer"):
        path = folder / "world/materials" / worker / "onboarding.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"Welcome {worker}")
    return folder


def test_default_dry_run_stages_launch_and_harness_without_external_effects(seeded, monkeypatch):
    from company_envs.world import hub_vm, hub_world, state_seed

    def forbidden(*args, **kwargs):
        pytest.fail("dry run dispatched an external effect")

    monkeypatch.setattr(state_seed, "seed_world", forbidden)
    monkeypatch.setattr(hub_world.CompanyWorld, "start", forbidden)
    monkeypatch.setattr(hub_vm, "HubWorkerVM", forbidden)
    monkeypatch.setattr(controller, "_MeteredModels", forbidden)
    before = {str(p.relative_to(seeded)): p.read_bytes() for p in seeded.rglob("*") if p.is_file()}
    report = run(seeded, DefaultSteps())
    assert report["status"] == "done"
    work = seeded / "runtime/tasks/task/controller/company"
    assert read(work / "runtime/episodes/task/own/episode-result.json")["reason"] == "all_done"
    grade = report["steps"]["grade:own"]["result"]
    assert grade["graded"] is False and grade["simulation_only"]
    # A dry run grades no business outcome, which this artifact has always promised to say.
    # "unmeasured" is that promise in the shared word of company_envs.receipt, and it is not a
    # rejection: _execute only stops a step whose result is False, so the run still completes. What
    # it stops is a reader counting a staged episode as a graded one.
    assert grade["outcome"] == "unmeasured" and grade["ok"] is None
    assert report["steps"]["grade:own"]["outcome"] == "unmeasured"
    assert report["steps"]["calibrate"]["outcome"] == "unmeasured", "nothing live to calibrate against"
    assert report["steps"]["teacher"]["outcome"] == "unmeasured", "and no feasibility proof was taken"
    assert report["steps"]["load"]["outcome"] == "passed", "a step that did its work still says so"
    assert (work / "runtime/vms/boss/guest/Desktop/ASSIGNMENT.md").exists()
    assert not (work / "runtime/vms/peer/guest/Desktop/ASSIGNMENT.md").exists()
    after = {
        str(p.relative_to(seeded)): p.read_bytes()
        for p in seeded.rglob("*")
        if p.is_file() and not p.is_relative_to(seeded / "runtime")
    }
    assert before == after
    again = run(seeded, DefaultSteps(), from_step="episode:own")
    assert again["status"] == "done"
    history = seeded / "runtime/tasks/task/controller/history"
    assert list(history.glob("*/episodes/task/own/episode-result.json"))
    reset = run(seeded, DefaultSteps(), reset=True)
    assert reset["sid"] != report["sid"]
    assert read(work / "runtime/sessions.json")["sid"] == reset["sid"]
    assert read(work / "runtime/vms/LAUNCH.json")["status"] == "dry_run"
    assert not (work / "runtime/episode.json").exists()
    assert run(seeded, DefaultSteps())["status"] == "done"


def test_default_refuses_implicit_boss(seeded):
    write(seeded / "tasks/task/workflow.json", {"worker_ids": ["boss", "peer"]})
    with pytest.raises(ControllerError, match="check:.*explicit known manager_id"):
        run(seeded, DefaultSteps())


def test_default_refuses_unimplemented_backend_before_vm_effects(seeded):
    with pytest.raises(ControllerError, match="launch:.*mypcbench"):
        run(seeded, DefaultSteps(), backend="mypcbench")
    assert not (seeded / "runtime/tasks/task/controller/company/runtime/vms").exists()


def test_real_grade_wiring_calibrates_before_grading_and_records_a_failing_grade(seeded, monkeypatch):
    from company_envs.world import grader

    run(seeded, DefaultSteps())
    work = seeded / "runtime/tasks/task/controller/company"
    write(work / "tasks/task/grader.json", {"authored": True})
    events = []

    def calibrate(folder, task, clients, *, golden=False, models=None):
        assert folder == work and task == "task" and set(clients) == {"demo_mock"}
        events.append("calibrate")
        return {"accepted": True}

    def grade(folder, task, clients, *, models, label):
        assert label == "own"
        events.append("grade")
        return {"passed": False, "score": 0}

    class GradeOnly(FakeSteps):
        def run(self, step, ctx):
            if step == "grade:own":
                ctx.state["options"]["dry_run"] = False
                try:
                    return DefaultSteps().run(step, ctx)
                finally:
                    ctx.state["options"]["dry_run"] = True
            return super().run(step, ctx)

    monkeypatch.setattr(controller, "_live", lambda ctx: None)
    monkeypatch.setattr(controller, "_MeteredModels", lambda ctx, **kw: object())
    monkeypatch.setattr(grader, "calibrate", calibrate)
    monkeypatch.setattr(grader, "grade", grade)
    write(work / "runtime/grades/task/calibration.json", {"accepted": True, "scope": "all_checks"})
    steps = GradeOnly()
    report = run(seeded, steps, from_step="grade")
    assert events == ["grade"] and steps.calls == ["teardown"]  # calibration is its own step now
    assert report["steps"]["grade:own"]["status"] == "done"
    assert report["steps"]["grade:own"]["result"]["passed"] is False
    assert report["policy_runs"] == {
        "own": {"score": 0, "passed": False, "environment_fault": False, "reason": "graded"}
    }


def test_bad_receipt_does_not_corrupt_failure_checkpoint(folder):
    class Bad(FakeSteps):
        def run(self, step, ctx):
            return {"unserializable": object()}

    with pytest.raises(ControllerError, match="load: TypeError"):
        run(folder, Bad())
    assert read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["load"]["status"] == "failed"


def test_retry_preserves_previous_artifact_bytes(folder):
    class Failure(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            if step == "grade:own":
                ctx.artifact("grade.json", {"original": "failed attempt"})
                raise RuntimeError("after artifact")
            return result

    with pytest.raises(ControllerError):
        run(folder, Failure())
    before = read(folder / "runtime/tasks/task/CONTROLLER.json")
    path = folder / "runtime/tasks/task" / before["steps"]["grade:own"]["artifacts"]["grade.json"]
    content = path.read_bytes()
    after = run(folder)
    assert path.read_bytes() == content
    assert after["steps"]["grade:own"]["artifacts"] != before["steps"]["grade:own"]["artifacts"]


def test_service_owner_resets_before_stopping_with_process_double(folder, monkeypatch):
    from types import SimpleNamespace

    from company_envs.world import hub_vm, hub_world

    events = []
    process = {"pid": 123, "start_ticks": "456", "boot_id": "test"}

    class World:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            events.append(("start", self.sid))

        def reset(self):
            events.append(("reset", self.sid))

        def stop(self):
            events.append(("stop", self.sid))

    work = folder / "runtime/tasks/task/controller/company"
    request = work / "runtime/SERVICE-REQUEST.json"
    write(
        request, {"folder": str(work), "root": str(folder.parents[1]), "hub_root": "unused", "sid": "new-sid"}
    )
    monkeypatch.setattr(hub_world, "CompanyWorld", World)
    monkeypatch.setattr(hub_vm, "_process_identity", lambda pid: process)
    monkeypatch.setattr(controller.signal, "signal", lambda *args: None)
    monkeypatch.setattr(controller.threading, "Event", lambda: SimpleNamespace(wait=lambda seconds: True))
    controller._service_main(request)
    assert events == [("start", "new-sid"), ("reset", "new-sid"), ("stop", "new-sid")]
    owner = {"sid": "new-sid", "process": process}
    assert read(work / "runtime/SERVICE.json") == owner
    assert read(work / "runtime/SERVICE-READY.json") == owner
    assert read(work / "runtime/SERVICE-STOPPED.json") == owner


def test_live_resume_reuses_owned_ready_service_without_launching(folder, monkeypatch):
    from company_envs.world import hub_vm

    state = {"task_id": "task", "sid": "live-sid", "options": {"dry_run": False}}
    ctx = controller.Context(folder.parents[1], folder, state, "serve", time.monotonic() + 10)
    owner = {"sid": "live-sid", "process": {"pid": 123, "start_ticks": "456", "boot_id": "test"}}
    for name in ("SERVICE.json", "SERVICE-READY.json"):
        write(ctx.work / "runtime" / name, owner)
    monkeypatch.setattr(hub_vm, "_process_identity", lambda pid: owner["process"])
    health_checks = []
    monkeypatch.setattr(controller, "_live", lambda ctx: health_checks.append(ctx.sid))

    def forbidden(*args, **kwargs):
        pytest.fail("resume must attach to the owned service")

    monkeypatch.setattr(controller.subprocess, "Popen", forbidden)
    assert controller._serve(ctx) == {"ok": True, **owner}
    assert health_checks == ["live-sid"]


def test_resume_finalizes_completed_reset_without_repeating_effects(folder, monkeypatch):
    run(folder)
    original = controller._execute

    def crash_after_reset(*args):
        original(*args)
        if args[-1] == "reset":
            raise KeyboardInterrupt("after reset checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_execute", crash_after_reset)
        with pytest.raises(KeyboardInterrupt):
            run(folder, reset=True)
    before = read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert before["reset_pending"] and before["steps"]["reset"]["status"] == "done"
    retry = FakeSteps()
    after = run(folder, retry)
    assert retry.calls == []
    assert after["status"] == "ready" and after["sid"] == before["sid"]


def test_episode_deadline_blocks_grade_and_teardown(folder):
    class SlowEpisode(FakeSteps):
        def run(self, step, ctx):
            if step == "episode:own":
                time.sleep(2)
            return super().run(step, ctx)

    budgets = copy.deepcopy(DEFAULT_BUDGETS)
    budgets["seconds"]["episode:own"] = 0.02
    with pytest.raises(ControllerError, match="episode:own: TimeoutError"):
        run(folder, SlowEpisode(), budgets=budgets)
    state = read(folder / "runtime/tasks/task/CONTROLLER.json")
    assert state["steps"]["episode:own"]["status"] == "failed"
    assert state["steps"]["grade:own"]["status"] == state["steps"]["teardown"]["status"] == "pending"
    # A failed episode step is an environment-class outcome until it is redone.
    assert state["policy_runs"]["own"]["environment_fault"] is True
    assert state["policy_runs"]["own"]["reason"].startswith("episode_failed: TimeoutError")


def test_resume_after_teardown_checkpoint_does_not_teardown_twice(folder, monkeypatch):
    original = controller._execute

    def crash_after_teardown(*args):
        original(*args)
        if args[-1] == "teardown":
            raise KeyboardInterrupt("after teardown checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(controller, "_execute", crash_after_teardown)
        with pytest.raises(KeyboardInterrupt):
            run(folder)
    retry = FakeSteps()
    assert run(folder, retry)["status"] == "done"
    assert retry.calls == []


def test_resume_after_discard_checkpoint_only_runs_teardown(folder):
    class Discard(FakeSteps):
        def run(self, step, ctx):
            if step == "teacher":
                write(ctx.work / "tasks/task/STATUS.json", {"status": "discarded"})
                return ctx.artifact("teacher.json", {"class": "systemic_defect", "score": 0})
            return super().run(step, ctx)

    with pytest.raises(KeyboardInterrupt):
        run(folder, Discard(crash_before="teardown"))
    before = read(folder / "runtime/tasks/task/CONTROLLER.json")
    resumed = FakeSteps()
    result = run(folder, resumed)
    assert resumed.calls == ["teardown"]
    assert result["steps"]["episode:own"] == before["steps"]["episode:own"]
    assert result["steps"]["teacher"] == before["steps"]["teacher"]


def test_resume_after_teacher_discard_persisted_before_step_checkpoint(folder, monkeypatch):
    from company_envs.world import teacher

    calls = []
    report = {
        "class": "systemic_defect",
        "score": None,
        "reason": "missing_record: no invoice",
        "task_id": "task",
    }

    def rollout(root, work, task, **kwargs):
        calls.append(task)
        write(work / "runtime/teacher/task/TEACHER.json", report)
        write(
            work / "tasks/task/STATUS.json",
            {
                "status": "discarded",
                "teacher_class": report["class"],
                "teacher_score": None,
                "teacher_report": "runtime/teacher/task/TEACHER.json",
            },
        )
        return report

    class Steps(FakeSteps):
        interrupted = False

        def run(self, step, ctx):
            if step == "teacher":
                result = DefaultSteps().run(step, ctx)
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt("before teacher step checkpoint")
                return result
            return super().run(step, ctx)

    monkeypatch.setattr(teacher, "teacher_rollout", rollout)
    steps = Steps()
    with pytest.raises(KeyboardInterrupt):
        run(folder, steps, dry_run=False)
    before = read(folder / "runtime/tasks/task/CONTROLLER.json")
    result = run(folder, steps, dry_run=False)
    assert calls == ["task"]
    assert before["steps"]["episode:own"]["status"] == "pending"
    # A discarded teacher is not a pass: the worker episode and grade are skipped, not run.
    assert result["steps"]["episode:own"]["result"] == BLOCKED_BY_TEACHER
    assert result["steps"]["grade:own"]["result"] == BLOCKED_BY_TEACHER
    assert result["steps"]["episode:own"]["outcome"] == "unmeasured"
    assert result["policy_runs"]["own"] == {
        "score": None,
        "passed": None,
        "environment_fault": False,
        "reason": "teacher_did_not_pass",
    }
    assert result["steps"]["teacher"]["result"] == report
    assert result["steps"]["teardown"]["status"] == "done"


@pytest.mark.parametrize("fail_export", [False, True])
def test_episode_exports_every_worker_before_stop_and_grade(seeded, monkeypatch, fail_export):
    from company_envs.world import grader, hub_vm
    from company_envs.world.backends import mypcbench
    from company_envs.world.harness import Action, FakeBackend

    events = []
    ctx = controller.Context(
        seeded.parents[1],
        seeded,
        {
            "task_id": "task",
            "options": {"dry_run": False, "backend": "mypcbench"},
            "model_calls_used": 0,
            "budgets": {"model_calls": 10},
        },
        "episode",
        time.monotonic() + 10,
    )
    shutil.copytree(seeded, ctx.work)
    (ctx.root / "config.toml").write_text("[models]\nexpand=['test/model']\n")
    monkeypatch.setattr(controller, "_live", lambda ctx: None)

    class Backend(FakeBackend):
        def __init__(self, record):
            super().__init__()
            self.worker = record.parent.name

        async def export_files(self, destination, *, deadline):
            assert events[:2] == ["done", "done"]
            assert "stop" not in events
            assert destination == ctx.work / "runtime/exports/task/own" / self.worker
            assert deadline > time.monotonic()
            events.append(f"export:{self.worker}")
            if fail_export:
                raise RuntimeError("download failed")
            write(destination / "manifest.json", {"files": {}})
            return {"status": "exported"}

    def policy(*args, **kwargs):
        def done(observation):
            events.append("done")
            return Action("done")

        return done

    monkeypatch.setattr(mypcbench, "MyPCBenchBackend", Backend)
    monkeypatch.setattr(mypcbench, "WorkerPolicy", policy)
    monkeypatch.setattr(hub_vm, "stop_company", lambda folder: events.append("stop"))
    steps = DefaultSteps()
    if fail_export:
        with pytest.raises(RuntimeError, match="download failed"):
            steps.episode(ctx)
        assert events == ["done", "done", "export:boss", "stop"]
    else:
        result = steps.episode(ctx)
        assert set(result["exports"]) == {"boss", "peer"}
        write(ctx.work / "runtime/endpoints.json", {"apps": {}})
        write(ctx.work / "tasks/task/grader.json", {})
        write(ctx.work / "runtime/grades/task/calibration.json", {"accepted": True, "scope": "all_checks"})
        monkeypatch.setattr(grader, "calibrate", lambda *args, **kwargs: events.append("calibrate"))
        monkeypatch.setattr(grader, "grade", lambda *args, **kwargs: events.append("grade"))
        monkeypatch.setattr(controller, "_MeteredModels", lambda ctx, **kw: object())
        steps.grade(ctx)
        assert events == [
            "done",
            "done",
            "export:boss",
            "export:peer",
            "stop",
            "grade",
        ]  # calibration is its own step


def test_fake_episode_archives_old_exports_and_skips_downloads(seeded, monkeypatch):
    from company_envs.world.backends import mypcbench

    def forbidden(*args, **kwargs):
        pytest.fail("fake episode must not download guest files")

    monkeypatch.setattr(mypcbench.MyPCBenchBackend, "export_files", forbidden)
    run(seeded, DefaultSteps())
    work = seeded / "runtime/tasks/task/controller/company"
    old = work / "runtime/exports/task/own/boss/Documents/old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("Old deliverable")
    other = work / "runtime/exports/task/macu/boss/Documents/theirs.txt"
    other.parent.mkdir(parents=True)
    other.write_text("Another policy's deliverable")
    run(seeded, DefaultSteps(), from_step="episode")
    assert not (work / "runtime/exports/task/own").exists()
    assert other.read_text() == "Another policy's deliverable"  # only this policy's outputs move
    history = seeded / "runtime/tasks/task/controller/history"
    archived = list(history.glob("*/exports/task/own/boss/Documents/old.txt"))
    assert len(archived) == 1 and archived[0].read_text() == "Old deliverable"


@pytest.mark.parametrize("outcome", ["teacher_failed", "systemic_defect"])
def test_failed_teacher_skips_the_worker_episode_and_grade(folder, outcome):
    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return result | {"class": outcome, "score": 0.0} if step == "teacher" else result

    steps = Steps()
    report = run(folder, steps)
    assert report["status"] == "done"
    assert steps.calls == ["load", "check", "serve", "calibrate", "teacher", "teardown"]
    for step in ("launch", "episode:own", "grade:own"):
        assert report["steps"][step]["result"] == BLOCKED_BY_TEACHER
        assert report["steps"][step]["outcome"] == "unmeasured", "a skip measured nothing"


def test_a_skipped_step_says_so_on_its_row_and_no_restart_may_begin_after_it(folder):
    """A skipped step wrote the receipt a completed one writes: status done, and the only signs that
    nothing ran were a result a reader had to look inside and a started of None. Measured 2026-09-10,
    the reader that was fooled is the restart guard: `--from episode:own` was accepted past a launch
    that never booted a VM, re-skipped the episode and the grade on the teacher's standing verdict,
    ran teardown alone and answered status done -- an operator asking for the episode again was told
    the run had finished and never told why it had not run."""

    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return result | {"class": "teacher_failed", "score": 0.0} if step == "teacher" else result

    report = run(folder, Steps())
    for step in ("launch", "episode:own", "grade:own"):
        row = report["steps"][step]
        assert row["status"] == "done" and row["skipped"] is True and row["started"] is None
    assert "skipped" not in report["steps"]["teacher"], "the teacher ran; only what it stopped is skipped"
    with pytest.raises(ValueError, match=r"launch \(skipped: teacher_did_not_pass\)"):
        run(folder, FakeSteps(), from_step="episode:own")
    # The resume rule is untouched: status stays done, so re-entering runs nothing at all rather
    # than re-skipping every step, and a restart at a step that really ran is still allowed.
    resumed = FakeSteps()
    assert run(folder, resumed)["status"] == "done" and resumed.calls == []
    again = FakeSteps()
    run(folder, again, from_step="teacher")
    assert again.calls == ["teacher", "launch", "episode:own", "grade:own", "teardown"]
    assert "skipped" not in read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["launch"]


def test_a_skip_the_operator_asked_for_does_not_block_a_restart(folder):
    """The other kind of skip. --no-teacher declines the safeguard and everything after it still runs,
    so refusing a later restart on the skipped teacher row would take away the one path that flag
    exists for. Only a skip that stopped the work after it too blocks one."""
    steps = FakeSteps()
    report = run(folder, steps, no_teacher=True)
    assert report["steps"]["teacher"]["skipped"] is True
    assert steps.calls == [s for s in STEP_NAMES if s != "teacher"]
    again = FakeSteps()
    run(folder, again, from_step="grade:own", no_teacher=True)
    assert again.calls == ["grade:own", "teardown"]


def test_nothing_downstream_counts_a_skipped_run_as_a_completed_one(folder):
    """The other half of the answer, pinned so a future reader cannot quietly start being fooled.
    One in four findings in this project was a measurement artifact, and this was nearly one: of the
    readers of a checkpoint, report.py never opens one, the cohort report takes `done` from
    AGREEMENT.json, the agreement's vm_verified requires TEACHER.json's class to be teacher_passed,
    and difficulty counts only runs that passed or failed. The skip already reported itself to every
    one of them through policy_runs."""
    from company_envs.world.agreement import difficulty

    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return result | {"class": "teacher_failed", "score": 0.0} if step == "teacher" else result

    report = run(folder, Steps())
    assert report["policy_runs"]["own"] == {
        "score": None,
        "passed": None,
        "environment_fault": False,
        "reason": "teacher_did_not_pass",
    }
    # No score, no pass, no fail: a skipped run labels nothing, rather than labelling a task hard
    # because a rollout nobody ran did not solve it.
    assert difficulty(report["policy_runs"]) == {"passed_by": [], "failed_by": [], "label": None}


def test_passed_teacher_runs_the_worker_episode(folder):
    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return result | {"class": "teacher_passed", "score": 1.0} if step == "teacher" else result

    steps = Steps()
    run(folder, steps)
    assert steps.calls == list(STEP_NAMES)


def test_archiving_an_episode_keeps_the_calibration_proof(folder):
    run(folder)  # a completed fake run gives a work copy and a runtime
    work = folder / "runtime" / "tasks" / "task" / "controller" / "company"
    grades = work / "runtime" / "grades" / "task"
    grades.mkdir(parents=True, exist_ok=True)
    (grades / "calibration.json").write_text('{"accepted": true, "scope": "all_checks"}')
    (grades / "judge_bench.json").write_text("{}")
    (grades / "report.json").write_text('{"score": 0.1}')  # the single-episode layout
    for policy in ("own", "macu"):
        (grades / policy).mkdir()
        (grades / policy / "report.json").write_text('{"score": 0.5}')
    (work / "runtime" / "episode.json").write_text("{}")
    state = read(folder / "runtime/tasks/task/CONTROLLER.json")
    ctx = controller.Context(folder.parents[1], folder, state, "episode:own", float("inf"))
    assert ctx.policy == "own"
    controller._archive_episode(ctx, ctx.policy)
    # Proofs of the initial world survive; the other policy's grade is not this episode's.
    assert (grades / "calibration.json").exists() and (grades / "judge_bench.json").exists()
    assert (grades / "macu" / "report.json").exists()
    assert not (grades / "report.json").exists() and not (grades / "own").exists()
    assert not (work / "runtime" / "episode.json").exists()
    history = folder / "runtime" / "tasks" / "task" / "controller" / "history"
    assert len(list(history.glob("*/grades/task/report.json"))) == 1
    assert len(list(history.glob("*/grades/task/own/report.json"))) == 1


TWO = ["own", "macu"]


def test_step_names_follow_the_configured_policies(tmp_path):
    assert controller.step_names(TWO) == (
        "load",
        "check",
        "serve",
        "calibrate",
        "teacher",
        "launch",
        "episode:own",
        "grade:own",
        "episode:macu",
        "grade:macu",
        "teardown",
    )
    assert STEP_NAMES == controller.step_names(["own"])
    assert controller.configured_policies(tmp_path) == ["own"]  # no config.toml
    (tmp_path / "config.toml").write_text('[worker_policy]\npolicies = ["own", "macu"]\n')
    assert controller.configured_policies(tmp_path) == TWO
    for bad in ([], ["own", "own"], ["Own"], "own"):
        with pytest.raises(ValueError, match="policies"):
            controller.step_names(bad)
    # Budgets follow the step list: a plain episode/grade entry covers every policy's pair.
    seconds = dict.fromkeys(("load", "check", "serve", "calibrate", "teacher", "launch", "teardown"), 5)
    names = controller.step_names(TWO)
    budgets = controller._budgets({"seconds": seconds | {"episode": 7, "grade": 8}, "model_calls": 0}, names)
    assert budgets["seconds"]["episode:macu"] == 7 and budgets["seconds"]["grade:own"] == 8
    assert "episode" not in budgets["seconds"]
    with pytest.raises(ValueError, match="every controller step"):
        controller._budgets({"seconds": seconds | {"episode:other": 1}, "model_calls": 0}, names)


def test_two_policies_get_two_episodes_with_a_fresh_session_between(folder):
    steps = FakeSteps()
    report = run(folder, steps, policies=TWO)
    assert report["status"] == "done"
    assert steps.calls == [
        "load",
        "check",
        "serve",
        "calibrate",
        "teacher",
        "launch",
        "episode:own",
        "grade:own",
        "reset",  # FakeSteps' reset relaunches, as the real one does
        "launch",
        "episode:macu",
        "grade:macu",
        "teardown",
    ]
    own, macu = report["sessions"]["own"], report["sessions"]["macu"]
    assert own["reset"] is False and macu["reset"] is True and own["sid"] != macu["sid"]
    assert (
        report["sid"] == macu["sid"] == read(folder / "runtime/tasks/task/controller/fake-apps.json")["sid"]
    )
    reset = report["steps"]["reset"]
    assert reset["status"] == "done" and reset["result"]["sid"] == macu["sid"]
    assert any(
        row.get("fresh_session_for") == "macu" and row["reset_from_sid"] == own["sid"]
        for row in report["history"]
    )
    assert set(report["policy_runs"]) == {"own", "macu"}
    assert report["options"]["policies"] == TWO
    resumed = FakeSteps()
    assert run(folder, resumed, policies=TWO) == report and resumed.calls == []
    with pytest.raises(ValueError, match="same task, options and budgets"):
        run(folder)  # a different policy list is a different run


def test_interrupted_policy_reset_resumes_with_the_same_reserved_sid(folder):
    with pytest.raises(ControllerError, match="reset:"):
        run(folder, FakeSteps(fail="reset"), policies=TWO)
    interrupted = read(folder / "runtime/tasks/task/CONTROLLER.json")
    reserved = interrupted["sessions"]["macu"]["sid"]
    assert interrupted["sid"] == reserved != interrupted["sessions"]["own"]["sid"]
    assert interrupted["steps"]["grade:own"]["status"] == "done"
    resumed = FakeSteps()
    report = run(folder, resumed, policies=TWO)
    assert resumed.calls == ["reset", "launch", "episode:macu", "grade:macu", "teardown"]
    assert report["sid"] == reserved and report["sessions"]["macu"]["sid"] == reserved


def test_from_a_later_policy_episode_gives_it_a_fresh_session(folder):
    before = run(folder, policies=TWO)
    steps = FakeSteps()
    after = run(folder, steps, from_step="episode:macu", policies=TWO)
    assert steps.calls == ["reset", "launch", "episode:macu", "grade:macu", "teardown"]
    assert after["sid"] != before["sid"] and after["sessions"]["own"] == before["sessions"]["own"]
    steps = FakeSteps()
    again = run(folder, steps, from_step="grade:macu", policies=TWO)
    assert steps.calls == ["grade:macu", "teardown"] and again["sid"] == after["sid"]
    with pytest.raises(ValueError, match="from_step"):
        run(folder, from_step="episode:nobody", policies=TWO)


def test_failed_teacher_skips_every_policy(folder):
    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return result | {"class": "teacher_failed", "score": 0.0} if step == "teacher" else result

    steps = Steps()
    report = run(folder, steps, policies=TWO)
    assert steps.calls == ["load", "check", "serve", "calibrate", "teacher", "teardown"]
    assert report["sessions"] == {}
    assert all(row["reason"] == "teacher_did_not_pass" for row in report["policy_runs"].values())


@pytest.mark.parametrize(
    ("grade", "episode", "fault"),
    [
        (
            {"score": 0.4, "passed": False, "wiped_collections": {"desk_mock": ["tickets"]}},
            None,
            "wiped_collections",
        ),
        ({"score": 0.0, "passed": False, "judge_unavailable": True}, None, "judge_unavailable"),
        ({"score": 1.0, "passed": True}, {"reason": "error"}, "episode_error"),
        (
            {"score": 1.0, "passed": True, "wiped_collections": {}, "judge_unavailable": False},
            {"reason": "all_done"},
            None,
        ),
    ],
)
def test_policy_runs_record_environment_faults_from_the_grade_report(folder, grade, episode, fault):
    class Steps(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            if step == "grade:own":
                return result | grade
            if step == "episode:own" and episode:
                return result | {"result": episode}
            return result

    report = run(folder, Steps())
    row = report["policy_runs"]["own"]
    assert row["score"] == grade["score"] and row["passed"] == grade["passed"]
    assert row["environment_fault"] is (fault is not None)
    assert row["reason"] == (fault or "graded")


def test_old_single_episode_checkpoint_migrates_and_resumes(folder):
    done = run(folder)
    path = folder / "runtime/tasks/task/CONTROLLER.json"
    state = read(path)
    # Rewrite the checkpoint the way the fixed-step controller wrote it, interrupted before grade.
    state["options"].pop("policies")
    state.pop("sessions")
    state.pop("policy_runs")
    state["steps"]["episode"] = state["steps"].pop("episode:own")
    state["steps"]["grade"] = controller._pending()
    state["steps"].pop("grade:own")
    state["steps"]["teardown"] = controller._pending()
    state["budgets"]["seconds"]["episode"] = state["budgets"]["seconds"].pop("episode:own")
    state["budgets"]["seconds"]["grade"] = state["budgets"]["seconds"].pop("grade:own")
    state["history"].append({"step": "grade", **controller._pending()})
    state.update(status="running", responsible_step="grade")
    write(path, state)
    steps = FakeSteps()
    resumed = run(folder, steps)
    assert steps.calls == ["grade:own", "teardown"]
    assert resumed["steps"]["episode:own"] == done["steps"]["episode:own"]
    assert "episode" not in resumed["steps"] and "grade" not in resumed["steps"]
    assert resumed["budgets"] == done["budgets"] and resumed["options"]["policies"] == ["own"]
    assert resumed["history"][-1]["step"] == "grade:own"
    assert resumed["policy_runs"]["own"]["reason"] == "graded"
    assert resumed["status"] == "done"


def test_unknown_policy_adapter_fails_only_when_selected(seeded, monkeypatch):
    from company_envs.world import hub_vm

    assert controller._worker_policy_class("own").__name__ == "WorkerPolicy"
    assert controller._worker_policy_class("macu").__name__ == "MacuPolicy"
    with pytest.raises(ModuleNotFoundError):
        controller._worker_policy_class("no_such_adapter")
    events = []
    ctx = controller.Context(
        seeded.parents[1],
        seeded,
        {
            "task_id": "task",
            "options": {"dry_run": False, "backend": "mypcbench", "policies": ["own", "no_such_adapter"]},
            "model_calls_used": 0,
            "budgets": {"model_calls": 10},
        },
        "episode:no_such_adapter",
        time.monotonic() + 10,
    )
    assert ctx.policy == "no_such_adapter"
    shutil.copytree(seeded, ctx.work)
    (ctx.root / "config.toml").write_text("[models]\nexpand=['test/model']\n")
    monkeypatch.setattr(controller, "_live", lambda ctx: None)
    monkeypatch.setattr(hub_vm, "stop_company", lambda folder: events.append("stop"))
    with pytest.raises(ModuleNotFoundError, match="policies.no_such_adapter"):
        DefaultSteps().episode(ctx)
    assert events == ["stop"]  # the VMs are still released


def test_each_task_gets_its_own_runtime_and_checkpoint(folder):
    """Two tasks of one company run independently: a checkpoint, a working copy and artifacts
    each, under runtime/tasks/<task>/, with nothing at the company-level location."""
    root = folder.parents[1]
    first, second = FakeSteps(), FakeSteps()
    one = run_company(root, folder, "task_a", budgets=DEFAULT_BUDGETS, steps=first)
    two = run_company(root, folder, "task_b", budgets=DEFAULT_BUDGETS, steps=second)
    assert first.calls == second.calls == list(STEP_NAMES)
    assert one["status"] == two["status"] == "done" and one["sid"] != two["sid"]
    for task, report in (("task_a", one), ("task_b", two)):
        runtime = folder / "runtime" / "tasks" / task
        assert controller.task_runtime(folder, task) == runtime
        assert read(runtime / "CONTROLLER.json") == report and report["task_id"] == task
        assert (runtime / "controller" / "fake-apps.json").is_file()
        for relative in report["steps"]["load"]["artifacts"].values():
            assert (runtime / relative).is_file()
    assert not (folder / "runtime" / "CONTROLLER.json").exists()
    assert not (folder / "runtime" / "controller").exists()
    assert controller.task_runtimes(folder) == {
        "task_a": folder / "runtime/tasks/task_a",
        "task_b": folder / "runtime/tasks/task_b",
    }
    # Options are pinned per task: a different backend for task_b is refused, task_a is untouched.
    with pytest.raises(ValueError, match="same task, options and budgets"):
        run_company(root, folder, "task_b", budgets=DEFAULT_BUDGETS, steps=FakeSteps(), dry_run=False)
    resumed = FakeSteps()
    assert run_company(root, folder, "task_a", budgets=DEFAULT_BUDGETS, steps=resumed) == one
    assert resumed.calls == []
    # Redoing task_b's episode archives under task_b's runtime only.
    again = run_company(
        root, folder, "task_b", budgets=DEFAULT_BUDGETS, steps=FakeSteps(), from_step="episode"
    )
    assert again["attempt"] != two["attempt"]
    assert not (folder / "runtime/tasks/task_a/controller/history").exists()


def test_a_checkpoint_at_the_old_location_is_resumed_in_place(folder):
    """A company that ran before the per-task layout keeps runtime/CONTROLLER.json and
    runtime/controller/ for the task named there; other tasks get per-task runtimes."""
    root = folder.parents[1]
    with pytest.raises(KeyboardInterrupt):
        run_company(root, folder, "task_a", budgets=DEFAULT_BUDGETS, steps=FakeSteps(crash_before="launch"))
    # Move the interrupted run to where the single-checkpoint controller kept it.
    shutil.move(folder / "runtime/tasks/task_a/CONTROLLER.json", folder / "runtime/CONTROLLER.json")
    shutil.move(folder / "runtime/tasks/task_a/controller", folder / "runtime/controller")
    shutil.rmtree(folder / "runtime/tasks")
    legacy = read(folder / "runtime/CONTROLLER.json")
    assert legacy["task_id"] == "task_a" and legacy["steps"]["serve"]["status"] == "done"
    assert controller.task_runtime(folder, "task_a") == folder / "runtime"
    assert controller.task_runtime(folder, "task_b") == folder / "runtime/tasks/task_b"
    assert controller.task_runtimes(folder) == {"task_a": folder / "runtime"}
    resumed = FakeSteps()
    report = run_company(root, folder, "task_a", budgets=DEFAULT_BUDGETS, steps=resumed)
    assert resumed.calls == ["launch", "episode:own", "grade:own", "teardown"]  # nothing replayed
    assert report["sid"] == legacy["sid"] and report["status"] == "done"
    assert read(folder / "runtime/CONTROLLER.json") == report  # read and written where it was
    assert not (folder / "runtime/tasks").exists()
    for relative in report["steps"]["teardown"]["artifacts"].values():
        assert (folder / "runtime" / relative).is_file()
    # A second task of the same company runs beside it, in the per-task layout.
    other = run_company(root, folder, "task_b", budgets=DEFAULT_BUDGETS, steps=FakeSteps())
    assert other["status"] == "done"
    assert read(folder / "runtime/tasks/task_b/CONTROLLER.json") == other
    assert read(folder / "runtime/CONTROLLER.json") == report
    assert controller.task_runtimes(folder) == {
        "task_a": folder / "runtime",
        "task_b": folder / "runtime/tasks/task_b",
    }
    # After the operator moves the old files into place, the task resumes from there.
    (folder / "runtime/tasks/task_a").mkdir()
    shutil.move(folder / "runtime/CONTROLLER.json", folder / "runtime/tasks/task_a/CONTROLLER.json")
    shutil.move(folder / "runtime/controller", folder / "runtime/tasks/task_a/controller")
    assert controller.task_runtime(folder, "task_a") == folder / "runtime/tasks/task_a"
    moved = FakeSteps()
    assert run_company(root, folder, "task_a", budgets=DEFAULT_BUDGETS, steps=moved) == report
    assert moved.calls == []
    # An unreadable or task-less company-level file is not anyone's checkpoint.
    (folder / "runtime/CONTROLLER.json").write_text("{not json")
    assert controller.task_runtime(folder, "task_a") == folder / "runtime/tasks/task_a"
    assert controller.task_runtimes(folder) == {
        "task_a": folder / "runtime/tasks/task_a",
        "task_b": folder / "runtime/tasks/task_b",
    }


def test_a_near_miss_is_a_high_score_that_ran_out_of_budget():
    from company_envs.world.controller import _near_miss

    out_of_time = {"score": 0.9, "trajectory_summary": {"episode": {"reason": "budget_exhausted"}}}
    assert _near_miss(out_of_time)
    assert not _near_miss({**out_of_time, "score": 0.6})
    assert not _near_miss({"score": 0.9, "trajectory_summary": {"episode": {"reason": "all_done"}}})
    assert not _near_miss({"score": None, "trajectory_summary": {"episode": {"reason": "budget_exhausted"}}})


def test_a_near_miss_and_an_environment_report_do_not_share_one_retry():
    """One counter for two rules made the near-miss branch unreachable for the company it was written
    for: its checkpoint carries teacher_env_retries 1, spent by the environment-report rule, against a
    teacher score of 0.90. A resume could not reach it either."""
    source = inspect.getsource(controller)
    assert "teacher_near_miss_retries" in source
    near = source.split("_near_miss(result)", 1)[1][:400]
    assert "teacher_near_miss_retries" in near
    environment = source.split("and signals", 1)[1][:400]
    assert "teacher_env_retries" in environment
    assert "teacher_near_miss_retries" not in environment


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (RuntimeError("grader unavailable"), "faulted"),
        (ValueError("the task needs a manager and specialists"), "refused"),
        (TimeoutError("step seconds budget exhausted"), "faulted"),
    ],
)
def test_a_failed_step_records_whether_re_running_it_can_possibly_help(folder, error, outcome):
    """`status: failed` says a step did not finish; it never said whether the world or the host was
    at fault, so the driver retried both kinds the same way.

    The row now carries the shared outcome from company_envs.receipt, by the split models.py already
    encodes in its exception hierarchy: a ValueError is a verdict about this world and re-running it
    reaches the same answer, while a dead socket, a spent budget and a blown deadline say nothing
    about the world at all. `status` is untouched, so the resume loop re-runs exactly what it did
    before -- what is new is that a reader can tell which of the two it is looking at.
    """

    class Failing(FakeSteps):
        def run(self, step, ctx):
            if step == "check":
                raise error
            return super().run(step, ctx)

    with pytest.raises(ControllerError, match="check:"):
        run(folder, Failing())
    row = read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["check"]
    assert row["status"] == "failed", "the resume loop's field is unchanged"
    assert row["outcome"] == outcome
    assert read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["serve"]["outcome"] is None


def test_a_step_that_ran_records_a_pass_and_one_whose_result_refuses_itself_records_the_refusal(folder):
    """Every row answers "what did this measure", including the ordinary ones: without that, the four
    outcomes would only ever appear on the unhappy paths and a reader would still have to know which
    stage wrote the row to interpret a missing field."""

    class RefusingLoad(FakeSteps):
        def run(self, step, ctx):
            result = super().run(step, ctx)
            return (
                result | {"ok": False, "reason": "the world is not this task's"} if step == "load" else result
            )

    report = run(folder)
    assert report["status"] == "done"
    assert {row["outcome"] for row in report["steps"].values() if row["status"] == "done"} == {"passed"}

    with pytest.raises(ControllerError, match="load:"):
        run(folder, RefusingLoad(), from_step="load")
    row = read(folder / "runtime/tasks/task/CONTROLLER.json")["steps"]["load"]
    assert row["status"] == "failed" and row["outcome"] == "refused", "the receipt's own verdict wins"
