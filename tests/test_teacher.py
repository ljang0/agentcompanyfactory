"""Teacher classification over real Episodes/graders and scripted offline effects."""

import copy
import json
from types import SimpleNamespace

import pytest

from company_envs.storage import now, read, write
from company_envs.world import controller, grader, release, teacher
from company_envs.world.harness import FakeBackend


@pytest.fixture
def company(tmp_path):
    root, folder = tmp_path, tmp_path / "company"
    (root / "config.toml").write_text('[models]\nexpand = ["codex/expand"]\nteacher = ["codex/strong"]\n')
    write(folder / "MANIFEST.json", {"company_id": "test"})
    write(folder / "company.json", {"workers": [{"id": "boss"}, {"id": "peer"}]})
    write(
        folder / "apps.json",
        {
            "apps": [
                {
                    "app_id": "app",
                    "schema": "schema.md",
                    "state_file": "world/app.state.json",
                    "top_level_keys": ["records"],
                }
            ]
        },
    )
    (root / "schema.md").write_text("records have id and status")
    write(folder / "world/app.state.json", {"records": [{"id": 1, "status": "open"}]})
    write(
        folder / "tasks/task/workflow.json",
        {
            "manager_id": "boss",
            "worker_ids": ["boss", "peer"],
            "success_criteria": [{"requirement": "Resolve record 1", "method": "state"}],
            "completion": {"feasible_path": {"secret": "PRIVATE-SOLUTION"}},
        },
    )
    write(folder / "tasks/task/assignment.json", {"brief": "Resolve record 1 with your colleague."})
    check = grader.Check(
        id="resolved",
        criterion_ref="/success_criteria/0",
        kind="state",
        description="Resolve record 1",
        predicate=grader.Predicate(
            app_id="app", selector="$.records[?(@.id==1)].status", operator="equals", value_json='"closed"'
        ),
    )
    author = SimpleNamespace(call=lambda *a: (grader.TaskGrader(checks=[check]), {"fake": True}))
    grader.author_grader(root, folder, "task", models=author)
    initial = teacher.FakeTeacherBackend()
    initial.prepare(folder, "task", ["boss", "peer"], {})
    grader.calibrate(
        folder,
        "task",
        initial.clients,
        reference_states={"app": {"records": [{"id": 1, "status": "closed"}]}},
    )
    return root, folder


class Script:
    def __init__(self, actions=None):
        self.actions = iter(actions or [{"name": "done"}])
        self.calls = []

    def call(self, job, prompt, schema, **kwargs):
        assert job == "teacher"
        assert "PRIVATE-SOLUTION" not in prompt
        self.calls.append(prompt)
        return schema.model_validate(next(self.actions, {"name": "done"})), {"fake": True}


class ScriptedBackend(teacher.FakeTeacherBackend):
    def __init__(self, *, failure=None, effect=False, action_result=None):
        self.failure, self.effect, self.action_result = failure, effect, action_result
        self.events = []

    def prepare(self, *args):
        self.events.append("prepare")
        if self.failure == "prepare":
            raise OSError("proxy/material delivery failure")
        super().prepare(*args)
        if self.failure == "seed":
            self.clients["app"].current = {}

    def backend(self, worker):
        owner = self

        class Backend(FakeBackend):
            async def observe(self, *, deadline):
                if owner.failure == "harness":
                    raise OSError("VM disappeared")
                return await super().observe(deadline=deadline)

            async def execute(self, action, *, deadline):
                if owner.effect:
                    owner.clients["app"].current["records"][0]["status"] = "closed"
                    log = owner.folder / "runtime/attribution/app.jsonl"
                    log.parent.mkdir(parents=True, exist_ok=True)
                    with log.open("a") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "worker_id": worker,
                                    "sid": owner.sid,
                                    "at": now(),
                                    "changed_keys": ["records"],
                                }
                            )
                            + "\n"
                        )
                return owner.action_result if owner.action_result is not None else {"ok": True}

        return Backend()

    def quiesce(self):
        self.events.append("quiesce")
        if self.failure == "quiesce":
            raise OSError("VM would not stop")

    def close(self):
        self.events.append("close")
        if self.failure == "close":
            raise OSError("proxy would not stop")


def rollout(company, backend=None, script=None, **budgets):
    root, folder = company
    return teacher.teacher_rollout(
        root,
        folder,
        "task",
        backend=backend or ScriptedBackend(),
        models=script or Script(),
        budgets={"seconds": 5, "actions": 10, "model_calls": 20, **budgets},
    )


@pytest.mark.parametrize("outcome", ["environment_error", "grader_error", "teacher_failed", "teacher_passed"])
def test_verified_failure_and_score_classes(company, monkeypatch, outcome):
    backend = ScriptedBackend(
        failure="harness" if outcome == "environment_error" else None, effect=outcome == "teacher_passed"
    )
    actions = [{"name": "click", "x": 1, "y": 2}, {"name": "done"}]
    if outcome == "grader_error":

        def broken(*args, **kwargs):
            assert backend.events[-1] == "quiesce"
            raise grader.CalibrationError("bad calibration")

        monkeypatch.setattr(grader, "grade", broken)
    result = rollout(company, backend, Script(actions))
    root, folder = company
    assert result["class"] == outcome
    assert result == read(folder / "runtime/teacher/task/TEACHER.json")
    assert result["trajectory_summary"]["episode"]
    assert result["evidence_refs"] and all((folder / ref).exists() for ref in result["evidence_refs"])
    assert backend.events == ["prepare", "quiesce", "close"]
    status = read(folder / "tasks/task/STATUS.json")
    assert status["teacher_score"] == result["score"]
    if outcome in teacher.DISCARD_CLASSES:
        assert status["status"] == "discarded" and status["reason"]
        with pytest.raises(ValueError, match="discarded task task"):
            release.bundle_company(root, folder, root / "out.tar.gz")
        assert not (root / "out.tar.gz").exists()
    else:
        assert status.get("status") != "discarded"
        assert result["score"] == (1 if outcome == "teacher_passed" else 0)
    if outcome == "teacher_passed":
        assert result["contributions"]["observed_workers"] == 1
        assert result["contributions"]["workers"][0]["passing_checks"] == ["resolved"]


@pytest.mark.parametrize("failure", ["prepare", "seed", "quiesce", "close"])
def test_environment_lifecycle_failures(company, failure):
    assert rollout(company, ScriptedBackend(failure=failure))["class"] == "environment_error"


@pytest.mark.parametrize(
    "summary,expected",
    [
        ("ENVIRONMENT_ERROR: URL unreachable", "teacher_failed"),
        ("SYSTEMIC_DEFECT: permission_wall: save denied", "teacher_failed"),
        ("SYSTEMIC_DEFECT: feature_absent: app cannot export", "teacher_failed"),
        ("Cannot complete: promised record is missing", "teacher_failed"),
        ("I could not find the answer in time", "teacher_failed"),
        ("No missing record or permission problem; finished", "teacher_failed"),
    ],
)
def test_done_signals(company, summary, expected):
    assert rollout(company, script=Script([{"name": "done", "summary": summary}]))["class"] == expected


@pytest.mark.parametrize(
    "count,result,expected",
    [
        (3, {"ok": False, "error": "save rejected"}, "teacher_failed"),
        (2, {"ok": False, "error": "save rejected"}, "teacher_failed"),
        (3, {"ok": True}, "teacher_failed"),
        (3, {"exit_code": 1, "stderr": "failed"}, "teacher_failed"),
        (3, {"ok": False, "error": "ERR_CONNECTION_REFUSED"}, "teacher_failed"),
    ],
)
def test_repeated_failed_actions(company, count, result, expected):
    # Use a one-worker roster so every scripted click belongs to the same worker.
    _, folder = company
    workflow = read(folder / "tasks/task/workflow.json")
    workflow["worker_ids"] = ["boss"]
    write(folder / "tasks/task/workflow.json", workflow)
    # Keep calibration pins valid after reducing this fixture's roster.
    draft = read(folder / "tasks/task/grader.json")
    from company_envs.storage import digest

    draft["grading_context_hash"] = digest(grader._grading_context(folder, "task"))
    write(folder / "tasks/task/grader.json", draft)
    proof_path = folder / "runtime/grades/task/calibration.json"
    proof = read(proof_path)
    proof["proof_key"] = grader._proof_key(draft, teacher.initial_states(folder))
    write(proof_path, proof)
    actions = [{"name": "click", "x": 1, "y": 2}] * count + [{"name": "done"}]
    report = rollout(company, ScriptedBackend(action_result=result), Script(actions))
    assert report["class"] == expected


@pytest.mark.parametrize(
    "report,expected",
    [
        (
            {"score": 0, "checks": [{"status": "fail", "reason": "ValueError: invalid selector"}]},
            "grader_error",
        ),
        ({"score": 0, "checks": [{"status": "error"}]}, "grader_error"),
        ({"score": 0, "errors": {"judgment": "bad response"}}, "grader_error"),
        ({"score": 0, "errors": {"app": "unreachable"}}, "environment_error"),
        ({"score": float("nan")}, "grader_error"),
        ({"score": 0.7}, "teacher_failed"),
    ],
)
def test_grade_error_and_score_classification(company, monkeypatch, report, expected):
    monkeypatch.setattr(grader, "grade", lambda *a, **kw: report)
    assert rollout(company)["class"] == expected


def test_threshold_and_existing_task_status(company, monkeypatch):
    folder = company[1]
    write(folder / "tasks/task/STATUS.json", {"status": "accepted", "review": "keep"})
    monkeypatch.setattr(grader, "grade", lambda *a, **kw: {"score": 0.7})
    assert rollout(company, threshold=0.7)["class"] == "teacher_passed"
    assert read(folder / "tasks/task/STATUS.json")["status"] == "accepted"
    assert rollout(company)["class"] == "teacher_failed"
    assert read(folder / "tasks/task/STATUS.json")["review"] == "keep"


def test_high_score_cannot_override_an_explicit_failed_gate(company, monkeypatch):
    monkeypatch.setattr(grader, "grade", lambda *a, **kw: {"score": 1.0, "passed": False})
    assert rollout(company)["class"] == "teacher_failed"


def test_staged_trial_copy_uses_population_reference_clock(company, tmp_path):
    from company_envs.world.hub_vm import reference_instant

    source = company[1]
    write(source / "world/POPULATION.json", {"reference_date": "2026-08-17"})
    target = tmp_path / "trial"
    teacher._snapshot(source, target, "task")
    assert reference_instant(target) == "2026-08-17T09:00:00"
    assert not (source / "world/SEED.json").exists()


def test_model_budget_and_attempt_isolation(company):
    model = Script()
    before = read(company[1] / "runtime/grades/task/calibration.json")
    first = rollout(company, script=model, model_calls=0)
    assert first["class"] == "teacher_failed" and first["model_calls_used"] == 0
    assert model.calls == []
    second = rollout(company, script=model)
    assert second["model"] == ["codex/strong"]
    assert first["evidence_refs"] != second["evidence_refs"]
    assert read(company[1] / "runtime/grades/task/calibration.json") == before
    assert all((company[1] / ref).exists() for ref in first["evidence_refs"])


def test_expand_fallback_and_model_override(company):
    (company[0] / "config.toml").write_text('[models]\nexpand = ["codex/expand"]\n')
    assert rollout(company)["model"] == ["codex/expand"]
    assert rollout(company, teacher_model="codex/override")["model"] == ["codex/override"]


def test_controller_resumes_teacher_and_skip_is_pinned(tmp_path):
    def run(steps, **kwargs):
        return controller.run_company(
            tmp_path, tmp_path / "company", "task", budgets=controller.DEFAULT_BUDGETS, steps=steps, **kwargs
        )

    with pytest.raises(KeyboardInterrupt):
        run(controller.FakeSteps(crash_before="teacher"))
    previous = read(tmp_path / "company/runtime/tasks/task/CONTROLLER.json")
    assert previous["steps"]["calibrate"]["status"] == "done"
    assert previous["steps"]["launch"]["status"] == "pending"  # the teacher runs before the worker VMs launch
    resumed = controller.FakeSteps()
    state = run(resumed)
    assert resumed.calls == ["teacher", "launch", "episode:own", "grade:own", "teardown"]
    assert state["steps"]["calibrate"] == previous["steps"]["calibrate"]
    assert state["sid"] == previous["sid"]
    with pytest.raises(ValueError, match="same task, options and budgets"):
        run(controller.FakeSteps(), no_teacher=True)
    replay = controller.FakeSteps()
    run(replay, from_step="teacher")
    assert replay.calls == ["teacher", "launch", "episode:own", "grade:own", "teardown"]


def test_controller_skip_and_legacy_checkpoint(tmp_path):
    folder = tmp_path / "company"
    budgets = copy.deepcopy(controller.DEFAULT_BUDGETS)
    budgets["seconds"].pop("teacher")
    steps = controller.FakeSteps()
    result = controller.run_company(tmp_path, folder, "task", budgets=budgets, steps=steps, no_teacher=True)
    assert "teacher" not in steps.calls
    assert result["steps"]["teacher"]["result"]["skipped"]
    path = folder / "runtime/tasks/task/CONTROLLER.json"
    result["options"].pop("no_teacher")
    result["options"].pop("teacher_model")
    result["steps"].pop("teacher")
    result["budgets"]["seconds"].pop("teacher")
    write(path, result)
    resumed = controller.FakeSteps()
    controller.run_company(tmp_path, folder, "task", budgets=budgets, steps=resumed)
    assert resumed.calls == ["teacher"]


def test_release_checks_controller_discard_before_pins(tmp_path):
    folder = tmp_path / "company"
    write(
        folder / "runtime/tasks/task/controller/company/tasks/task/STATUS.json",
        {"status": "discarded", "reason": "environment_error"},
    )
    with pytest.raises(ValueError, match="discarded task task"):
        release.bundle_company(tmp_path, folder, tmp_path / "bundle.tar.gz")


def test_live_wiring_with_doubled_services_vms_and_model_process(company, monkeypatch):
    from company_envs.world import hub_app, hub_vm, hub_world
    from company_envs.world.backends import mypcbench

    root, folder = company
    (root / "config.toml").write_text(
        '[models]\nexpand = ["codex/expand"]\nteacher = ["codex/strong"]\n[design]\nhub_root = "hub"\n'
    )
    inputs = {"base_image": "base", "browser_dir": "browser", "host_ip": "192.0.2.1"}
    write(folder / "runtime/vms/LAUNCH.json", {"inputs": inputs})
    events, configs, prompts = [], [], []
    memory = teacher.FakeTeacherBackend()

    class World:
        def __init__(self, work, hub, cache, **kwargs):
            self.folder = work
            assert work != folder and work.is_relative_to(folder / "runtime/teacher")
            assert hub == root / "hub"

        def start(self):
            events.append("start")
            memory.prepare(self.folder, "task", ["boss", "peer"], {})
            self.endpoints = {"app": {"harness_url": "http://fake"}}

        def stop(self):
            events.append("close")

    def launch(work, task_id, **kwargs):
        events.append("launch")
        assert kwargs == {**inputs, "workers": ["boss", "peer"], "dry_run": False}

    async def infer(config, prompt, image, directory, deadline, **kwargs):
        configs.append(config)
        prompts.append(prompt)
        return {"name": "done", "summary": "No solution found within budget"}, {"fake": True}

    monkeypatch.setattr(hub_world, "CompanyWorld", World)
    monkeypatch.setattr(hub_app, "HubClient", lambda url: memory.clients["app"])
    monkeypatch.setattr(hub_vm, "launch_company", launch)
    monkeypatch.setattr(hub_vm, "stop_company", lambda work: events.append("quiesce"))
    monkeypatch.setattr(teacher, "MyPCBenchBackend", lambda path: FakeBackend())
    monkeypatch.setattr(mypcbench, "_model_process", infer)
    report = teacher.teacher_rollout(
        root, folder, "task", backend="mypcbench", budgets={"seconds": 5, "model_calls": 10}
    )
    assert report["class"] == "teacher_failed"
    assert report["simulation_only"] is False  # Adapter flag, not a live certification.
    assert events == ["start", "launch", "quiesce", "close"]
    assert len(configs) == report["model_calls_used"] == 2
    assert all(c["models"]["worker_policy"] == ["codex/strong"] for c in configs)
    packets = [json.loads(prompt.split("\n")[-1]) for prompt in prompts]
    by_worker = {p["worker"]: p for p in packets}
    assert by_worker["boss"]["public_brief"] == "Resolve record 1 with your colleague."
    assert by_worker["peer"]["public_brief"] is None
    assert all("PRIVATE-SOLUTION" not in p for p in prompts)
    episode = report["trajectory_summary"]["episode"]
    assert set(episode["workers"]) == {"boss", "peer"}
    episode_path = next(folder / p for p in report["evidence_refs"] if p.endswith("/episode.json"))
    assert read(episode_path)["direct_messages"] is True


@pytest.mark.parametrize("outcome", ["teacher_failed", "systemic_defect"])
def test_default_controller_teacher_receipts_do_not_block_teardown(tmp_path, monkeypatch, outcome):
    seen = []

    def teacher_double(root, folder, task_id, *, backend, budgets, **kwargs):
        seen.append((folder, backend, budgets["teacher_model"]))
        budgets.get("reserve", lambda: None)()
        return {"class": outcome, "score": 0}

    class Steps(controller.FakeSteps):
        def run(self, step, context):
            if step == "teacher":
                return controller.DefaultSteps().run(step, context)
            return super().run(step, context)

    monkeypatch.setattr(teacher, "teacher_rollout", teacher_double)
    budgets = copy.deepcopy(controller.DEFAULT_BUDGETS)
    budgets["seconds"]["teacher"] = 4
    budgets["model_calls"] = 2
    steps = Steps()
    result = controller.run_company(
        tmp_path,
        tmp_path / "company",
        "task",
        budgets=budgets,
        steps=steps,
        dry_run=False,
        teacher_model="codex/override",
    )
    assert result["status"] == "done" and steps.calls[-1] == "teardown"
    assert result["model_calls_used"] == 0  # the teacher spends its own budget, not the episode's
    assert result["steps"]["teacher"]["result"]["class"] == outcome
    assert seen == [(tmp_path / "company/runtime/tasks/task/controller/company", "fake", "codex/override")]


def test_interruption_releases_backend_and_does_not_discard(company):
    class Interrupted(ScriptedBackend):
        def prepare(self, *args):
            self.events.append("prepare")
            raise KeyboardInterrupt("interrupted during startup")

    backend = Interrupted()
    with pytest.raises(KeyboardInterrupt, match="interrupted"):
        rollout(company, backend)
    assert backend.events == ["prepare", "quiesce", "close"]
    assert not (company[1] / "tasks/task/STATUS.json").exists()


class LoadTimeWriteBackend(ScriptedBackend):
    """A backend whose apps normalise their state the moment a browser opens them."""

    def launch(self):
        self.events.append("launch")
        self.clients["app"].current = {"records": [{"id": 1, "status": "closed", "colour": "#039BE5"}]}


def test_seed_is_read_back_before_the_browsers_open(company):
    """A clone that merges its own defaults under the seed on first load is not a broken world.

    The apps are read back between seeding and launching the VMs for exactly this reason: after a
    browser has opened an app, the stored state carries that app's normalisation and no longer
    equals the seed, which would fail every world on looks alone.
    """
    backend = LoadTimeWriteBackend(effect=True)
    report = rollout(company, backend, Script([{"name": "click", "x": 1, "y": 2}]))
    assert report["class"] != "environment_error"
    assert backend.events.index("prepare") < backend.events.index("launch")


def test_later_success_does_not_clear_discard(company):
    assert rollout(company, ScriptedBackend(failure="seed"))["class"] == "environment_error"
    assert (
        rollout(company, ScriptedBackend(effect=True), Script([{"name": "click", "x": 1, "y": 2}]))["class"]
        == "teacher_passed"
    )
    assert read(company[1] / "tasks/task/STATUS.json")["status"] == "discarded"


@pytest.mark.parametrize(
    "summary",
    [
        "SYSTEMIC_DEFECT: missing_record: impossible because record 1 is missing",
        "Cannot complete: promised record is missing",
        "ENVIRONMENT_ERROR: URL unreachable",
    ],
)
def test_policy_cannot_discard_a_task_by_claiming_impossibility(company, summary):
    result = rollout(company, script=Script([{"name": "done", "summary": summary}]))
    assert result["class"] == "teacher_failed"
    assert result["score"] == 0
    assert read(company[1] / "tasks/task/STATUS.json").get("status") != "discarded"
    assert any(summary in w["done_reasons"] for w in result["trajectory_summary"]["workers"].values())


def test_policy_controlled_tool_output_cannot_discard_task(company):
    backend = ScriptedBackend(action_result={"exit_code": 1, "stderr": "ERR_CONNECTION_REFUSED"})
    script = Script([{"name": "bash", "command": "printf ERR_CONNECTION_REFUSED >&2; exit 1"}] * 6)
    assert rollout(company, backend, script)["class"] == "teacher_failed"


@pytest.mark.parametrize(
    "action",
    [{"name": "invented"}, {"name": "send_message", "recipient": "unknown", "text": "hello"}],
)
def test_invalid_policy_actions_fail_the_teacher_without_discarding_task(company, action):
    result = rollout(company, script=Script([action]))
    assert result["class"] == "teacher_failed"
    assert read(company[1] / "tasks/task/STATUS.json").get("status") != "discarded"


def test_insider_note_renders_only_the_workers_golden_steps(tmp_path):
    import json

    from company_envs.world.teacher import insider_note

    task = tmp_path / "tasks" / "t1"
    task.mkdir(parents=True)
    (task / "golden.json").write_text(
        json.dumps(
            [
                {
                    "worker_id": "boss",
                    "app_id": "slack_mock",
                    "action": "message",
                    "text": "Ana: fill the sheet",
                },
                {
                    "worker_id": "ana",
                    "app_id": "google_sheets_mock",
                    "action": "set_current",
                    "state_patch": {"spreadsheets": {"sheet-1": {"id": "sheet-1", "name": "Costs"}}},
                },
            ]
        )
    )
    note = insider_note(tmp_path, "t1", "ana")
    assert "INSIDER REFERENCE" in note and "sheet-1" in note and "fill the sheet" not in note
    assert "fill the sheet" in insider_note(tmp_path, "t1", "boss")
    assert insider_note(tmp_path, "t1", "nobody") == ""
    assert insider_note(tmp_path, "missing", "ana") == ""


def test_teacher_failure_with_worker_environment_reports_is_retried_once(tmp_path, monkeypatch):
    from company_envs.world import controller, teacher

    calls = []

    def rollout(root, work, task, **kwargs):
        calls.append(task)
        return {
            "class": "teacher_failed",
            "score": 0.7,
            "reason": {
                "grade": {},
                "unverified_environment": [{"worker": "w", "reason": "app stuck loading"}],
            },
            "task_id": task,
        }

    monkeypatch.setattr(teacher, "teacher_rollout", rollout)
    monkeypatch.setattr(controller, "_calibrated_grader", lambda ctx: (None, None))

    class Steps(controller.FakeSteps):
        def run(self, step, ctx):
            if step == "teacher":
                ctx.state["options"]["dry_run"] = False
                ctx.state["options"]["backend"] = "mypcbench"
                try:
                    return controller.DefaultSteps().run(step, ctx)
                finally:
                    ctx.state["options"]["dry_run"] = True
                    ctx.state["options"]["backend"] = "fake"
            return super().run(step, ctx)

    folder = tmp_path / "companies" / "demo"
    run = lambda: controller.run_company(
        tmp_path, folder, "task", budgets=controller.DEFAULT_BUDGETS, steps=Steps()
    )
    with pytest.raises(controller.ControllerError, match="retrying once"):
        run()
    state = read(controller.task_runtime(folder, "task") / "CONTROLLER.json")
    assert state["teacher_env_retries"] == 1 and state["steps"]["teacher"]["status"] == "failed"
    report = run()  # the second rollout's failure stands
    assert calls == ["task", "task"] and report["steps"]["teacher"]["result"]["class"] == "teacher_failed"


def test_a_provider_outage_that_ends_the_episode_is_not_a_teacher_verdict():
    from company_envs.world.teacher import _provider_outage

    result = {
        "reason": "error",
        "workers": {
            "boss": {"error": None},
            "clerk": {
                "error": "RuntimeError: worker model failed: ModelUnavailable: codex/gpt: provider process failed: process exited 1"
            },
        },
    }
    assert "ModelUnavailable" in _provider_outage(result)
    assert (
        _provider_outage({"reason": "error", "workers": {"boss": {"error": "RuntimeError: open_url"}}})
        is None
    )
    assert _provider_outage(None) is None


def test_a_spent_call_budget_is_a_budget_and_not_a_decision(company):
    """A rollout that runs out of model calls used to answer ``done`` on the workers' behalf.

    That wrote the same trace a team which believed it had finished writes: the episode reason became
    ``all_done``, so a rollout that ran out of calls at a high score was indistinguishable from one
    that stopped on purpose, and the near-miss rule written to give exactly that rollout one more
    attempt could never see it. ``BudgetExhausted`` is what the harness has for this; it ends the
    worker, the episode goes on, and the receipt says a budget ended it.
    """
    result = rollout(company, model_calls=1)
    episode = result["trajectory_summary"]["episode"]
    assert episode["reason"] == "budget_exhausted"
    assert "model_budget" in {row["reason"] for row in episode["workers"].values()}
    assert all(
        "budget exhausted" not in reason
        for row in result["trajectory_summary"]["workers"].values()
        for reason in row["done_reasons"]
    )
    assert result["budget"]["model_calls"] == 1
    assert result["budget"]["episode_reason"] == "budget_exhausted"


def test_a_rollout_with_no_calls_says_it_was_never_funded(company):
    """A score of zero cannot tell "tried and failed" from "never tried".

    Measured: a rollout launched without ``--model-calls`` booted five VMs, every worker's first step
    hit the cap and the receipt read ``teacher_failed``, score 0.0, threshold 1.0 -- character for
    character what a world the reference solution cannot finish writes.
    """
    result = rollout(company, model_calls=0)
    assert result["class"] == "teacher_failed" and result["score"] == 0.0
    assert result["budget"]["model_calls"] == 0 and result["budget"]["model_calls_used"] == 0
    assert result["retryable"] == "unfunded"


def test_the_receipt_names_a_near_miss_so_it_can_be_given_another_rollout():
    """The teacher is the feasibility gate and a failure it records freezes the task, so the receipt
    has to say whether the rollout reached a verdict or ran out of something. trex-company sat at
    ``teacher_failed``, score 0.90, five workers out of time.
    """
    from company_envs.world.teacher import NEAR_MISS_SCORE, retry_reason

    out_of_time = {"episode_reason": "budget_exhausted", "model_calls": 60, "model_calls_used": 60}
    assert retry_reason("teacher_failed", 0.9, out_of_time) == "near_miss"
    assert retry_reason("teacher_failed", NEAR_MISS_SCORE, out_of_time) == "near_miss"
    assert retry_reason("teacher_failed", 0.6, out_of_time) is None
    assert retry_reason("teacher_passed", 0.9, out_of_time) is None
    assert retry_reason("teacher_failed", None, out_of_time) is None
    # A verdict reached on its own terms is a verdict, however low.
    assert retry_reason("teacher_failed", 0.9, {**out_of_time, "episode_reason": "all_done"}) is None
    # One worker's own fault (a dead transport, a provider that stopped answering) while its peers
    # worked on: the grade is of a short-handed team, not of the task.
    assert (
        retry_reason("teacher_failed", 0.2, {**out_of_time, "episode_reason": "worker_error"})
        == "worker_fault"
    )
    assert retry_reason("teacher_failed", 0.9, {**out_of_time, "model_calls": 0}) == "unfunded"
