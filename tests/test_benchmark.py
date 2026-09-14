"""Benchmark selection, evidence and rates with offline controller doubles."""

import copy
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

from company_envs.storage import read, write
from company_envs.world import benchmark as runner
from company_envs.world import controller


def make_companies(root):
    """Three small seeded companies, each with two tasks sharing one family."""
    companies = root / "companies"
    for index, cid in enumerate(("cedar", "birch", "ash")):
        folder = companies / cid
        workers = ["boss", "sales", "ops"]
        write(folder / "MANIFEST.json", {"company_id": cid, "source_run": f"run{index}"})
        write(
            folder / "company.json",
            {
                "sector": "services" if index < 2 else "manufacturing",
                "workers": [{"id": w, "title": w} for w in workers],
                "software": [{"id": "desk", "catalog_app_ids": ["desk_mock"]}],
            },
        )
        write(
            folder / "apps.json",
            {
                "workers": workers,
                "apps": [
                    {
                        "app_id": "desk_mock",
                        "hub_seedable": True,
                        "state_file": "world/desk_mock.state.json",
                        "top_level_keys": ["records"],
                        "identity_key": None,
                    }
                ],
            },
        )
        entries = []
        for task in ("task_b", "task_a"):
            write(
                folder / "tasks" / task / "workflow.json",
                {
                    "worker_ids": workers,
                    "manager_id": "boss",
                    "software_requirement_ids": ["desk"],
                    "feature_cell": {
                        "collections": ["desk_mock.records"],
                        "decision_type": "allocate" if index < 2 else "approve",
                    },
                    **({"difficulty": "hard"} if task == "task_b" else {}),
                },
            )
            write(folder / "tasks" / task / "assignment.json", {"brief": "Resolve the order with your team."})
            entries.append({"company_id": cid, "workflow_id": task, "family_id": f"family{index}"})
        write(
            root / "runs" / f"run{index}" / "dataset.json",
            {
                "split_rule": runner.FAMILY_RULE,
                "workflows": entries,
            },
        )
        write(folder / "world/desk_mock.state.json", {"records": []})
        write(folder / "world/world.json", {})
        write(folder / "world/identities.json", {})
        write(folder / "world/SEED.json", {"reference_date": "2026-09-07"})
        write(folder / "world/worker_apps.json", {w: ["desk_mock"] for w in workers})
        for worker in workers:
            path = folder / "world/materials" / worker / "welcome.md"
            path.parent.mkdir(parents=True)
            path.write_text(f"Welcome {worker}")
    return companies


@pytest.fixture
def companies(tmp_path):
    return make_companies(tmp_path)


def run(companies, out=None, **kwargs):
    return runner.benchmark(
        companies.parent,
        companies,
        kwargs.pop("split", "all"),
        kwargs.pop("agent", "teacher"),
        kwargs.pop("model", "codex/gpt-6-astra"),
        out or companies.parent / "out",
        **kwargs,
    )


def test_split_families_are_disjoint_stable_and_complete(companies):
    root = companies.parent
    select = lambda split: runner._select(root, companies, split)
    train, test, all_tasks = select("train"), select("test"), select("all")
    assert train and test
    assert {t["family_id"] for t in train}.isdisjoint(t["family_id"] for t in test)
    assert sorted(train + test, key=lambda t: (t["company_id"], t["task_id"])) == all_tasks
    assert [(t["company_id"], t["task_id"]) for t in all_tasks] == [
        (c, t) for c in ("ash", "birch", "cedar") for t in ("task_a", "task_b")
    ]
    # A second run using the same family stays on the same side of the split.
    path = root / "runs/run2/dataset.json"
    dataset = read(path)
    for row in dataset["workflows"]:
        row["family_id"] = "family0"
    dataset["workflows"].reverse()
    write(path, dataset)
    for split in ("train", "test"):
        selected = select(split)
        assert (any(t["company_id"] == "ash" for t in selected)) == (
            any(t["company_id"] == "cedar" for t in selected)
        )
        assert select(split) == selected


def test_disk_includes_unlisted_tasks_without_a_dataset(companies):
    folder = companies / "ash"
    write(folder / "tasks/orphan/workflow.json", {"decision_type": "review"})
    assert len(runner._select(companies.parent, companies, "all")) == 6
    for path in (companies.parent / "runs").glob("*/dataset.json"):
        path.unlink()
    tasks = runner._select(companies.parent, companies, "disk")
    assert len(tasks) == 7 and tasks[0]["task_id"] == "orphan"


@pytest.mark.parametrize(
    "change,error",
    [
        ({"split_rule": "choose a random task"}, "unsupported split rule"),
        ({"family_id": None}, "missing family_id"),
        ({"workflow_id": "../escape"}, "invalid identifier"),
        ({"duplicate": True}, "duplicate task"),
    ],
)
def test_invalid_dataset_is_rejected(companies, change, error):
    path = companies.parent / "runs/run0/dataset.json"
    dataset = read(path)
    if "split_rule" in change:
        dataset.update(change)
    elif change.get("duplicate"):
        dataset["workflows"].append(dataset["workflows"][0])
    else:
        dataset["workflows"][0].update(change)
    write(path, dataset)
    with pytest.raises(ValueError, match=error):
        run(companies, split="train")


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"split": "other"}, "split must be"),
        ({"agent": "other"}, "only supported agent"),
        ({"model": "no-provider"}, "provider/model"),
        ({"dry_run": False}, "requires VM inputs"),
        ({"budgets": {}}, "budgets requires"),
    ],
)
def test_invalid_options_fail_before_running(companies, kwargs, error):
    with pytest.raises(ValueError, match=error):
        run(companies, **kwargs)
    assert not (companies.parent / "out").exists()


def test_dry_run_and_resume_preserve_source_and_all_evidence(companies, monkeypatch):
    from company_envs.world import hub_vm, hub_world, state_seed, teacher

    def forbidden(*args, **kwargs):
        pytest.fail("dry run attempted a model, VM, app build or teacher rollout")

    monkeypatch.setattr(state_seed, "seed_world", forbidden)
    monkeypatch.setattr(hub_world.CompanyWorld, "start", forbidden)
    monkeypatch.setattr(hub_vm, "HubWorkerVM", forbidden)
    monkeypatch.setattr(teacher, "teacher_rollout", forbidden)
    monkeypatch.setattr(controller, "_MeteredModels", forbidden)
    before = {p: p.read_bytes() for p in companies.rglob("*") if p.is_file()}
    result = run(companies)
    assert result["overall"] == {"tasks": 6, "attempted": 0, "successes": 0, "success_rate": None}
    assert result["failure_classes"] == {"not_evaluated": 6}
    assert sum(r["steps"] for r in result["results"]) == 30
    assert result["by"]["difficulty"]["hard"]["tasks"] == 3
    assert result["by"]["app"]["desk_mock"]["tasks"] == 6
    out = companies.parent / "out"
    for row in result["results"]:
        trace = [json.loads(line) for line in (out / row["trajectory"]).read_text().splitlines()]
        assert [r["step_index"] for r in trace] == list(range(5))
        assert {r["worker"] for r in trace} == {"boss", "sales", "ops"}
        assert all((out / r["screenshot_path"]).is_file() for r in trace)
        assert all(r["app_state_diff_summary"] is None for r in trace)
        assert all(r["result_summary"] is not None for r in trace)
    files = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    monkeypatch.setattr(controller, "run_company", forbidden)
    assert run(companies) == result
    assert files == {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert before == {p: p.read_bytes() for p in companies.rglob("*") if p.is_file()}


def test_changed_resume_and_forged_result_rejected(companies):
    run(companies)
    with pytest.raises(ValueError, match="resume requires"):
        run(companies, model="codex/different")
    path = companies.parent / "out/tasks/ash/task_a/RESULT.json"
    saved = read(path)
    saved["options_hash"] = "another run"
    write(path, saved)
    with pytest.raises(ValueError, match="result does not match"):
        run(companies)


class OutcomeSteps(controller.FakeSteps):
    """Keep the real controller journal while supplying task outcomes and traces."""

    outcomes = (
        "teacher_passed",
        "teacher_failed",
        "environment_error",
        "grader_error",
        "systemic_defect",
        None,
    )
    events: ClassVar[list] = []

    def run(self, step, ctx):
        self.events.append((ctx.folder.parent.parent.name, ctx.state["task_id"], step))
        cid = read(ctx.folder / "MANIFEST.json")["company_id"]
        index = ["ash", "birch", "cedar"].index(cid) * 2 + (ctx.state["task_id"] == "task_b")
        outcome = self.outcomes[index]
        if outcome is None and step == "check":
            raise RuntimeError("world cannot be loaded")
        if step == "teacher":
            runtime = ctx.work / "runtime/teacher" / ctx.state["task_id"] / "runs/current/company/runtime"
            for worker in ("z_peer", "boss"):
                trace = runtime / "vms" / worker / "trace/events.jsonl"
                trace.parent.mkdir(parents=True)
                events = [
                    {"event": "screen", "file": "screen.png", "text": "x" * 1400},
                    {"event": "observation", "brief": "Resolve the order", "remaining_actions": 10},
                    {
                        "event": "action",
                        "number": 1,
                        "elapsed": 0.5,
                        "name": "click",
                        "arguments": {"x": 1, "y": 2},
                    },
                ]
                if worker == "boss":
                    events.append({"event": "result", "number": 1, "result": {"ok": True}})
                    (trace.parent / "screen.png").write_bytes(b"fake png")
                trace.write_text("\n".join(json.dumps(e) for e in events))
            write(
                runtime / "grades" / ctx.state["task_id"] / "evidence.json",
                {
                    "checks": [
                        {
                            "evidence": [
                                {
                                    "app_id": "desk_mock",
                                    "selector": "$.records",
                                    "state_diff": {"status": ["open", "closed"]},
                                }
                            ]
                        }
                    ],
                },
            )
            receipt = {
                "class": outcome,
                "score": 1 if outcome == "teacher_passed" else 0,
                "simulation_only": False,
                "trajectory_runtime": str(runtime.relative_to(ctx.work)),
            }
            ctx.artifact("teacher.json", receipt)
            # The benchmark must use the persisted artifact, not this return value.
            return {"class": "teacher_passed", "score": 1}
        return super().run(step, ctx)

    def teardown(self, ctx):
        self.events.append((ctx.folder.parent.parent.name, ctx.state["task_id"], "cleanup"))


def live_options():
    budgets = copy.deepcopy(controller.DEFAULT_BUDGETS)
    budgets["model_calls"] = 10
    return {
        "dry_run": False,
        "base_image": "base",
        "browser_dir": "browser",
        "host_ip": "192.0.2.1",
        "budgets": budgets,
    }


def test_rates_failure_classes_and_teacher_artifact_trajectory(companies, monkeypatch):
    OutcomeSteps.events.clear()
    monkeypatch.setattr(runner, "_TeacherSteps", OutcomeSteps)
    result = run(companies, **live_options())
    assert result["overall"] == {"tasks": 6, "attempted": 6, "successes": 1, "success_rate": 1 / 6}
    assert result["failure_classes"] == {
        "controller_error": 1,
        "environment_error": 1,
        "grader_error": 1,
        "systemic_defect": 1,
        "teacher_failed": 1,
    }
    assert result["by"]["sector"]["manufacturing"]["success_rate"] == 0.5
    assert result["by"]["decision_type"]["allocate"]["success_rate"] == 0
    assert result["by"]["difficulty"]["hard"]["success_rate"] == 0
    assert result["by"]["app"]["desk_mock"]["success_rate"] == 1 / 6
    assert ("cedar", "task_b", "cleanup") not in OutcomeSteps.events
    out = companies.parent / "out"
    row = result["results"][0]
    trace = [json.loads(line) for line in (out / row["trajectory"]).read_text().splitlines()]
    assert [r["worker"] for r in trace] == ["boss", "z_peer"]
    assert all(len(r["observation_summary"]) <= 1000 for r in trace)
    assert trace[0]["screenshot_path"] and trace[1]["screenshot_path"] is None
    assert trace[0]["result_summary"] and trace[1]["result_summary"] is None
    assert trace[0]["app_state_diff_summary"] is None
    summary = trace[1]["app_state_diff_summary"]
    assert "not attributed" in summary["scope"]
    assert summary["diffs"][0]["diff"] == '{"status": ["open", "closed"]}'
    assert (out / summary["evidence_path"]).is_file()
    assert "16.7%" in (out / "RESULTS.md").read_text()


def test_teacher_policy_wiring_keeps_baseline_failure_from_blocking_teacher(tmp_path, monkeypatch):
    from company_envs.world import hub_vm, teacher

    ctx = SimpleNamespace(dry_run=False, work=tmp_path / "work")
    stopped = []
    monkeypatch.setattr(hub_vm, "stop_company", lambda work: stopped.append(work))
    steps = runner._TeacherSteps()
    assert steps.episode(ctx)["skipped"] and stopped == [ctx.work]
    monkeypatch.setattr(controller.DefaultSteps, "grade", lambda self, ctx: {"passed": False, "score": 0})
    assert steps.grade(ctx) == {"ok": True, "baseline": {"passed": False, "score": 0}}
    seen = []

    def rollout(root, folder, task, **kwargs):
        seen.append((folder, task, kwargs["backend"], kwargs["budgets"]["teacher_model"]))
        return {"class": "teacher_failed", "score": 0}

    monkeypatch.setattr(teacher, "teacher_rollout", rollout)
    monkeypatch.setattr(controller, "_calibrated_grader", lambda ctx: (None, None))
    state = {
        "options": {"dry_run": False, "backend": "mypcbench", "teacher_model": "codex/chosen"},
        "task_id": "task",
        "budgets": {"model_calls": 10},
        "model_calls_used": 0,
    }
    ctx = controller.Context(tmp_path, tmp_path / "company", state, "teacher", float("inf"))
    assert steps.teacher(ctx)["class"] == "teacher_failed"
    assert seen == [(ctx.work, "task", "mypcbench", "codex/chosen")]


def test_interrupt_resumes_completed_results_without_repeating_tasks(companies, monkeypatch):
    original = controller.run_company
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 2:
            raise KeyboardInterrupt("before next task")
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "run_company", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(companies)
    assert read(companies.parent / "out/RESULTS.json")["overall"]["tasks"] == 1
    calls.clear()
    monkeypatch.setattr(
        controller, "run_company", lambda *a, **kw: (calls.append(a[2]), original(*a, **kw))[1]
    )
    assert run(companies)["overall"]["tasks"] == 6
    assert len(calls) == 5


def test_cleanup_failure_stops_batch_and_blocks_resume(companies, monkeypatch):
    class BrokenCleanup(OutcomeSteps):
        def run(self, step, ctx):
            if step == "serve":
                raise RuntimeError("setup failed")
            return super().run(step, ctx)

        def teardown(self, ctx):
            raise RuntimeError("owned VM is still running")

    monkeypatch.setattr(runner, "_TeacherSteps", BrokenCleanup)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        run(companies, **live_options())
    report = read(companies.parent / "out/RESULTS.json")
    assert report["overall"]["tasks"] == 1
    assert "owned VM" in report["results"][0]["cleanup_error"]
    with pytest.raises(RuntimeError, match="recorded cleanup failure"):
        run(companies, **live_options())


@pytest.mark.parametrize(
    "receipt,expected,attempted",
    [
        ({"ok": True}, "missing_teacher_report", 6),
        ({"class": "teacher_passed", "simulation_only": True}, None, 0),
    ],
)
def test_no_receipt_or_simulated_pass_cannot_count_as_success(
    companies, monkeypatch, receipt, expected, attempted
):
    class Receipts(controller.FakeSteps):
        def run(self, step, ctx):
            if step == "teacher":
                return ctx.artifact("teacher.json", receipt)
            return super().run(step, ctx)

    monkeypatch.setattr(runner, "_TeacherSteps", Receipts)
    report = run(companies, **live_options())
    assert report["overall"]["successes"] == 0
    assert report["overall"]["attempted"] == attempted
    assert all(not row["success"] and row["failure_class"] == expected for row in report["results"])


def test_controller_failure_after_startup_cleans_up_and_continues(companies, monkeypatch):
    cleaned = []

    class BrokenEpisode(controller.FakeSteps):
        def run(self, step, ctx):
            if step == "episode:own":
                raise RuntimeError("worker transport failed")
            return super().run(step, ctx)

        def teardown(self, ctx):
            cleaned.append(ctx.folder)

    monkeypatch.setattr(runner, "_TeacherSteps", BrokenEpisode)
    report = run(companies, **live_options())
    assert len(cleaned) == 6
    assert report["failure_classes"] == {"controller_error": 6}
    # The teacher runs first and here is a simulation, so no task counts as a live attempt.
    assert report["overall"]["success_rate"] is None


def test_empty_selection_and_missing_directory(tmp_path):
    companies = tmp_path / "companies"
    with pytest.raises(ValueError, match="does not exist"):
        run(companies)
    companies.mkdir()
    result = run(companies)
    assert result["overall"]["success_rate"] is None
    assert result["overall"]["tasks"] == 0
    assert result["failure_classes"] == {}


def test_output_and_evidence_paths_cannot_escape(companies, tmp_path):
    with pytest.raises(ValueError, match="separate directories"):
        run(companies, out=companies / "out")
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "out"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        run(companies)
    with pytest.raises(ValueError, match="escapes"):
        runner._inside(tmp_path, "../outside")
    state = {"task_id": "task", "steps": {"teacher": {"artifacts": {"teacher.json": "elsewhere.json"}}}}
    with pytest.raises(ValueError, match="outside controller artifacts"):
        runner._artifact(tmp_path, state, "teacher")


def test_snapshot_rejects_source_symlinks_and_recovers_incomplete_copy(companies, tmp_path):
    source, target = companies / "ash", tmp_path / "copy"
    target.mkdir()
    (target / "partial").write_text("interrupted copy")
    runner._snapshot(source, target, "task_a")
    assert not (target / "partial").exists()
    assert list((target / "tasks").iterdir()) == [target / "tasks/task_a"]
    assert not (target / "runtime").exists()
    link = source / "world/link"
    link.symlink_to(source / "company.json")
    with pytest.raises(ValueError, match="input symlink"):
        runner._snapshot(source, tmp_path / "other_copy", "task_a")
