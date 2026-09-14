"""The batch driver, exercised on a temporary company folder with fake commands and no model calls."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from company_envs.world.bulk_layer import BULK_VERSION

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "batch_companies.py"
spec = importlib.util.spec_from_file_location("batch_companies", SCRIPT)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

CLOCK = 1_800_000_000
# A BULK.json is only fresh when it records the expander that wrote it: the step is gated on the
# version, not on the file existing, so a world laid by older code is laid again.
LAID = {"bulk_version": BULK_VERSION}
# Every tuple of repo source files the driver compares a marker against. Named here so a new one
# added to deciding_code() is pinned by the fixtures too, instead of quietly letting the repo's own
# checkout times decide what these tests measure.
CODE_TUPLES = (
    "PROXY_CODE",
    "RENDER_CODE",
    "VISUAL_CODE",
    "CHECK_CODE",
    "READABILITY_CODE",
    "NAMES_CODE",
    "SYNC_CODE",
    "SHEET_CODE",
    "AGREEMENT_CODE",
    "REPAIR_CODE",
    "SERVE_CODE",
)


def stamped(module):
    """The freshness value MODULE records in the marker it writes, so a fixture's marker reads current.

    The markers these fixtures build now have to answer "which code decided you", the way the real
    ones do: storage.decided_by over the module's own DECIDES_THIS. It is deliberately the *real*
    module's files and not the tuples the fixtures pin -- the two halves are independent, one asking
    what the content says and one what the dates say, and a test that wants staleness can move either.
    """
    from importlib import import_module

    from company_envs.storage import decided_by

    return {"decided_by": decided_by(import_module(f"company_envs.world.{module}").DECIDES_THIS)}


def sheet_text(module="review_sheet"):
    """A REVIEW-SHEET.md body that records the module that drew it; the sheet is the non-JSON marker."""
    from importlib import import_module

    return import_module(f"company_envs.world.{module}").DECIDED_BY_LINE % stamped(module)["decided_by"]


def put(path, data=None, *, age=0, base=CLOCK):
    """Write PATH with an mtime AGE seconds after BASE, the base instant.

    A dict is written as JSON, a string as itself -- REVIEW-SHEET.md is a marker and is Markdown,
    and its recorded freshness value lives in its text rather than in a field.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data) if data is not None else "x")
    os.utime(path, (base + age, base + age))
    return path


@pytest.fixture
def folder(tmp_path):
    company = tmp_path / "companies" / "acme"
    put(company / "tasks" / "t1" / "workflow.json", {"id": "t1"})
    return company


def seeded(folder, status="seeded_reviewed", ok=True, age=10):
    """A company that went through export, rewrite, apps, seed and mine, with the given verdicts."""
    put(folder / "company.json", {}, age=age)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=age + 1)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=age + 2)
    put(folder / "world/SEED.json", {"status": status}, age=age + 3)
    if status == "seeded_reviewed":
        put(folder / "world/NAMES.json", stamped("names"), age=age + 4)
        put(folder / "world/BULK.json", LAID, age=age + 5)
        put(folder / driver.MINE, {"mined_at": "then", "requested": 0}, age=age + 6)
        put(folder / "world/worker_apps.json", {}, age=age + 7)
        put(folder / "REPORT-readability.json", stamped("readability"), age=age + 8)
        put(folder / "world/CHECKS.json", {"ok": ok, **stamped("world_check")}, age=age + 9)
    return folder


WORKERS = ("lead", "picker", "packer")


def workflow(outline, prepared=True):
    """A task as author-tasks writes it, and as the two preparation passes leave it."""
    body = {
        "outline_id": outline,
        "manager_id": "lead",
        "worker_ids": list(WORKERS),
        "contributions": [{"worker_id": w, "work": "x"} for w in WORKERS],
    }
    return body | ({"plain_brief": {}, "worker_apps": {}} if prepared else {})


def mineable(folder, designed=("o1", "o2", "o3", "o4", "o5"), published=("o1", "o2"), age=10):
    """A seeded, accepted world with DESIGNED outlines of which PUBLISHED already carry a task.

    Its check stage has not run yet: mining comes first, on the pass right after the world is
    seeded, laid with bulk and accepted by its reviewers. This world is dated an hour ago rather
    than at CLOCK because the driver writes the mine receipt itself, off the real clock: only a
    world older than now can show that the receipt does not date over the markers before it.
    """
    hour_ago = time.time() - 3600
    stamp = lambda path, data, offset: put(path, data, age=offset, base=hour_ago)
    shutil.rmtree(folder / "tasks")
    stamp(folder / "company.json", {"outlines": [{"id": o} for o in designed]}, age)
    for outline in published:
        stamp(folder / "tasks" / f"acme_{outline}" / "workflow.json", workflow(outline), age)
        stamp(folder / "tasks" / f"acme_{outline}" / "assignment.json", {"brief": "plainly"}, age)
    stamp(folder / "tasks/_plain_brief/REWRITE.json", {}, age + 1)
    stamp(folder / "tasks/_worker_apps/ASSIGN.json", {}, age + 2)
    stamp(folder / "world/SEED.json", {"status": "seeded_reviewed"}, age + 3)
    stamp(folder / "world/worker_apps.json", {w: ["gmail_mock"] for w in WORKERS}, age + 4)
    stamp(folder / "world/NAMES.json", stamped("names"), age + 5)
    stamp(folder / "world/BULK.json", LAID, age + 6)
    return folder


def with_target(monkeypatch, target):
    """config.toml's design table as the mine and vm stages read it."""
    design = {
        "vm_base_image": "img",
        "vm_browser_dir": "dir",
        "vm_host_ip": "10.0.2.2",
        "vm_guests": 20,
        "tasks_per_company": target,
    }
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": design})


def test_staleness_rule(tmp_path):
    marker, older, newer, missing = (tmp_path / n for n in ("marker", "older", "newer", "missing"))
    assert not driver.fresh(marker, [])  # missing
    put(marker, age=10)
    put(older, age=5)
    put(newer, age=20)
    assert driver.fresh(marker, [])
    assert driver.fresh(marker, [older, missing])  # inputs that do not exist are ignored
    assert not driver.fresh(marker, [older, newer])  # older than one of its inputs
    assert not driver.fresh(marker, [], accept=lambda data: data.get("accepted") is True)


def test_calibrate_rule_needs_accepted_and_all_checks(folder):
    seeded(folder)
    assert driver.position(folder) == ("render", "pending")  # the browser render comes before calibration
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=20)
    assert driver.position(folder) == ("visual", "pending")  # then a judge looks at what it rendered
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=25)
    report = folder / "runtime/grades/t1/calibration.json"
    put(report, {"accepted": True, "scope": "state_only"}, age=30)
    assert driver.position(folder) == ("calibrate", "pending")  # a state-only proof does not count
    put(report, {"accepted": False, "scope": "all_checks"}, age=30)
    assert driver.position(folder) == ("calibrate", "pending")
    put(report, {"accepted": True, "scope": "all_checks"}, age=30)
    assert driver.position(folder) == ("vm", "pending")
    # A calibration is a proof about records, so the world's states are what make it stale -- not
    # the report that judged them. world_check.py changed four times on the night of 2026-09-09 and
    # nothing on disk was re-dated; now that the checker is an input of the check stage, re-dating a
    # calibration on a newer CHECKS.json would recalibrate and re-run the VM episodes of 60 worlds
    # whose records never moved, every time someone edits the checker.
    put(folder / "world/CHECKS.json", {"ok": True, **stamped("world_check")}, age=40)
    assert driver.position(folder) == ("vm", "pending")  # an opinion recomputed, no record touched
    assert folder / "world/CHECKS.json" not in driver.paths(folder, driver.INPUTS["calibrate"])
    put(folder / "world/gmail_mock.state.json", {"emails": []}, age=45)  # a repair rewrote a record
    assert driver.position(folder) == ("render", "pending")  # the records changed: both stages redo
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=50)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=51)
    assert driver.position(folder) == ("calibrate", "pending")


def test_terminal_artifacts_stop_a_company(folder):
    seeded(folder, status="seeded_review_failed")
    assert driver.position(folder) == ("seed", "review_not_accepted")
    seeded(folder, ok=False)
    assert driver.position(folder) == ("check", "check_failed")
    seeded(folder, ok=True)
    put(folder / "tasks/t1/CALIBRATION_FAILED.json", {"attempts": ["no"]})
    assert driver.position(folder) == ("calibrate", "calibration_rejected")


def test_one_ungradeable_task_does_not_retire_its_company(folder):
    """Worlds average close to two tasks, and three companies were retired whole for one bad one.

    Both reference pipelines flag the failing artifact and keep the rest of the corpus. A company
    is terminal only when nothing is left to carry forward.
    """
    seeded(folder, ok=True)
    put(folder / "tasks/t2/workflow.json", {"id": "t2"})
    assert driver.tasks(folder) == ["t1", "t2"]

    put(folder / "tasks/t1/CALIBRATION_FAILED.json", {"attempts": ["no"]})
    assert driver.tasks(folder) == ["t2"]  # the survivor carries the company
    assert driver.position(folder) == ("render", "pending")

    put(folder / "tasks/t2/GRADER_FAILED.json", {"reason": "selects a whole collection"})
    assert driver.tasks(folder) == []
    assert driver.position(folder) == ("calibrate", "calibration_rejected")


def test_a_grader_the_author_refused_to_write_stops_the_company(folder):
    """Refusing to write checks is an answer about the task, and it has to be a receipt.

    Without one the driver finds no grader.json, runs calibrate again, and runs it again on every
    pass. 955 such failures were logged across eight companies, 390 on a single world that had
    already cleared seeding, review, checks, rendering and the visual judge.
    """
    seeded(folder, ok=True)
    assert driver.position(folder) == ("render", "pending")
    put(folder / "tasks/t1/GRADER_FAILED.json", {"reason": "selects a whole collection"})
    assert driver.position(folder) == ("calibrate", "grader_not_authored")
    assert "grader_not_authored" in driver.TERMINAL  # the driver stops rather than retrying
    put(folder / "tasks/t1/grader.json", {"checks": []})  # a later author succeeded
    assert driver.position(folder) == ("render", "pending")


def test_seed_failures_leave_an_artifact_and_stop_after_three(folder, monkeypatch, tmp_path):
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    commands = []
    monkeypatch.setattr(driver, "run", lambda cmd, log, timeout: commands.append(cmd) or 1)
    monkeypatch.setattr(driver, "seeding_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "prepare_work", lambda ctx, folder, log: tmp_path / "work")
    ctx = {"run": "r1", "dir": tmp_path / "batch", "work": tmp_path / "cache"}
    for count in (1, 2, 3):
        assert driver.position(folder, ctx, tmp_path / "acme.log") == ("seed", 1)
        assert driver.load(folder / "world/SEED_FAILED.json")["count"] == count
    assert driver.position(folder, ctx, tmp_path / "acme.log") == ("seed", "seed_failed_repeatedly")
    assert driver.position(folder) == ("seed", "seed_failed_repeatedly")  # visible without running anything
    assert len(commands) == 3 and all(cmd[3] == "seed-world" for cmd in commands)


def test_a_seed_killed_from_outside_keeps_its_attempts(folder, monkeypatch, tmp_path):
    """A signal from outside this driver stopped the seeder; it is not a verdict on the world."""
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    monkeypatch.setattr(driver, "run", lambda cmd, log, timeout: -2)
    monkeypatch.setattr(driver, "seeding_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "prepare_work", lambda ctx, folder, log: tmp_path / "work")
    ctx = {"run": "r1", "dir": tmp_path / "batch", "work": tmp_path / "cache"}
    for _ in range(4):
        assert driver.position(folder, ctx, tmp_path / "acme.log") == ("seed", "seed_interrupted")
    assert not (folder / "world/SEED_FAILED.json").exists()
    assert driver.position(folder) == ("seed", "pending")  # never retired


class FakeServe:
    """Stands in for the hub-serve process: endpoints.json appears at once, nothing to stop."""

    def __init__(self, cmd, **kwargs):
        folder = Path(cmd[cmd.index("hub-serve") + 1])
        put(folder / "runtime/endpoints.json", {"apps": {}})  # far in the future: newer than the serve start

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def fake_cli(folder, commands, results=None):
    """A run() double that records each command and writes the marker its real counterpart would."""
    writes = {
        "export-company": ("company.json", {}),
        "seed-world": ("world/SEED.json", {"status": "seeded_reviewed"}),
        "dedupe-names": ("world/NAMES.json", stamped("names")),
        "add-bulk": ("world/BULK.json", LAID),
        "readability": ("REPORT-readability.json", stamped("readability")),
        "world-check": ("world/CHECKS.json", {"ok": True, **stamped("world_check")}),
        # A real repair receipt records the calls it made; an empty `receipts` means it made none.
        "world-repair": ("world/REPAIR.json", {"rounds": 1, "receipts": {"world": [{"job": "x"}]}}),
        "review-repair": ("world/REVIEW-REPAIR.json", {"rounds": 1, "ok": False}),
        "render-check": (
            "runtime/RENDER.json",
            {"ok": True, "browser": "/usr/bin/google-chrome", "apps": {}, **stamped("render_check")},
        ),
        "visual-judge": (
            "runtime/VISUAL.json",
            {"ok": True, "apps": {}, "desktops": {}, **stamped("visual_judge")},
        ),
        "review-sheet": ("REVIEW-SHEET.md", sheet_text()),
        "agreement": (
            "AGREEMENT.json",
            {"done": True, "checked_at": "2026-09-08T00:00:00+00:00", **stamped("agreement")},
        ),
    }
    # The two preparation passes stamp the field they write into every task they are given, and
    # write their company-wide marker only when they were given the whole company (no --only).
    stamps = {
        "rewrite-briefs": ("plain_brief", driver.REWRITE),
        "assign-apps": ("worker_apps", driver.ASSIGN),
    }

    tick = [100]  # every marker written is newer than the one before, as on a real disk

    def run(cmd, log, timeout):
        name = cmd[3]
        only = cmd[cmd.index("--only") + 1 :] if "--only" in cmd else None
        label = f"run-company {cmd[5]}" if name == "run-company" else name
        commands.append(f"{label} --only" if only is not None else label)
        with open(log, "a") as stream:
            stream.write(f"\n$ {' '.join(map(str, cmd))}  [{driver.now()}]\n")
        rc = (results or {}).get(commands[-1], (results or {}).get(name, 0))
        if isinstance(rc, list):  # one exit code per call of that command, in order
            rc = rc.pop(0) if rc else 0
        if rc != 0:
            return rc
        tick[0] += 1
        if name in stamps:
            field, marker = stamps[name]
            for task in driver.tasks(folder) if only is None else only:
                path = folder / "tasks" / task / "workflow.json"
                put(path, {**driver.load(path), field: {"at": "then"}}, age=tick[0])
            if only is None:
                put(folder / marker, {}, age=tick[0])
        elif name == "author-tasks":  # one task per unused outline, unprepared, as authored
            count = int(cmd[cmd.index("--count") + 1])
            used = {driver.load(p).get("outline_id") for p in folder.glob("tasks/*/workflow.json")}
            spare = [o["id"] for o in driver.load(folder / "company.json")["outlines"] if o["id"] not in used]
            for outline in spare[:count]:
                task = folder / "tasks" / f"{folder.name}_{outline}"
                put(task / "workflow.json", workflow(outline, prepared=False), age=tick[0])
                put(task / "assignment.json", {"brief": "as authored"}, age=tick[0])
        elif name == "sync-worker-apps":  # keeps the world's workers, re-derives the apps they hold
            current = driver.load(folder / "world/worker_apps.json")
            put(folder / "world/worker_apps.json", {w: ["gmail_mock"] for w in current}, age=tick[0])
        elif name == "calibrate":  # per task: calibrate <folder> <task> --golden
            put(
                folder / f"runtime/grades/{cmd[5]}/calibration.json",
                {"accepted": True, "scope": "all_checks"},
                age=tick[0],
            )
        elif name == "author-golden":  # per task: its own reference trajectory
            put(folder / f"tasks/{cmd[5]}/golden.json", {}, age=tick[0])
        elif name == "run-company":  # per task: its own checkpoint under runtime/tasks/<task>/
            put(
                folder / f"runtime/tasks/{cmd[5]}/CONTROLLER.json",
                {"status": "done", "task_id": cmd[5]},
                age=tick[0],
            )
        else:
            rel, data = writes[name]
            put(folder / rel, data, age=tick[0])
        return rc

    return run


@pytest.fixture
def fake_pipeline(folder, monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands))
    monkeypatch.setattr(driver, "seeding_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "running_elsewhere", lambda folder, task: False)
    monkeypatch.setattr(driver, "calibrating_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "prepare_work", lambda ctx, folder, log: tmp_path / "work")
    monkeypatch.setattr(driver.subprocess, "Popen", FakeServe)
    monkeypatch.setattr(driver.time, "sleep", lambda seconds: None)
    real_slot = driver.vm_slot
    monkeypatch.setattr(
        driver, "vm_slot", lambda guests, budget, slot_dir=None: real_slot(guests, budget, tmp_path / "slots")
    )
    design = {"vm_base_image": "img", "vm_browser_dir": "dir", "vm_host_ip": "10.0.2.2", "vm_slots": 1}
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": design})
    # The repo's own files are inputs of the cheap markers (hub_patches.json and the proxy for
    # RENDER.json, the checker for CHECKS.json, the seeder for worker_apps.json, and so on through
    # deciding_code()). Pin every one of them so a checkout time or a fix landing mid-test-run plays
    # no part in what these tests measure: the cohort's markers were all re-dated inside 73 seconds
    # on 2026-09-10 by a copy of companies/, which is exactly how an mtime rule goes wrong.
    # Dated at the epoch, which is older than every marker any test writes -- including the ones
    # dated off the real clock an hour ago, which CLOCK is four months ahead of.
    monkeypatch.setattr(driver, "HUB_PATCHES", put(tmp_path / "hub_patches.json", {}, age=0, base=0))
    for name in CODE_TUPLES:
        monkeypatch.setattr(driver, name, (put(tmp_path / f"{name.lower()}.py", None, age=0, base=0),))
    ctx = {"run": "r1", "dir": tmp_path / "batch", "work": tmp_path / "cache"}
    ctx["dir"].mkdir()
    return commands, ctx, ctx["dir"] / "acme.log"


def test_one_pass_runs_every_stage_in_order_then_nothing(folder, fake_pipeline):
    commands, ctx, log = fake_pipeline
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == [
        "export-company", "rewrite-briefs", "assign-apps", "seed-world", "dedupe-names", "add-bulk",
        "sync-worker-apps", "readability", "world-check", "render-check", "visual-judge",
        "author-golden", "calibrate", "run-company t1", "review-sheet", "agreement",
    ]  # fmt: skip
    commands.clear()
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == []
    lines = [line for line in log.read_text().splitlines() if line.startswith("$ ")]
    assert len(lines) == 16 and lines[0].startswith("$ ") and lines[0].endswith("+00:00]")
    assert " export-company r1 acme  [" in lines[0]


def test_spare_outlines_are_mined_and_the_new_tasks_go_through_the_rest_of_the_pipeline(
    folder, fake_pipeline, monkeypatch
):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)  # five outlines designed, two published: three spare, and room for two
    assert driver.unused_outlines(folder) == ["o3", "o4", "o5"]
    assert driver.position(folder) == ("check", "pending")  # its own tasks come first
    assert driver.position(folder, ctx, log) == ("done", 0)
    # The two new tasks are authored against the accepted world and given the same preparation the
    # first two had, by id. Their sync leaves worker_apps.json newer than BULK, so the check stage
    # has nothing left to sync.
    mined = commands[commands.index("author-tasks") :]
    assert mined[:5] == [
        # One author-tasks call per task: the authoring contract is exact, so a batch fails whole
        # while single calls bank each task that lands.
        "author-tasks", "author-tasks", "rewrite-briefs --only", "assign-apps --only",
        "sync-worker-apps",
    ]  # fmt: skip
    assert driver.tasks(folder) == ["acme_o1", "acme_o2", "acme_o3", "acme_o4"]
    assert driver.unused_outlines(folder) == ["o5"]  # the target, not the design, decided how many
    for task in ("acme_o3", "acme_o4"):
        assert {"plain_brief", "worker_apps"} <= set(driver.load(folder / "tasks" / task / "workflow.json"))
    assert set(driver.load(folder / "world/worker_apps.json")) == set(WORKERS)
    # Mining runs last, so the world's own two tasks are calibrated and run before it designs any
    # more; the pass then picks the mined pair up and carries them through the same stages.
    # Mining runs last, so this pass delivers the world's own two tasks and only then designs more.
    assert [c for c in commands if c.startswith("run-company")] == [
        "run-company acme_o1",
        "run-company acme_o2",
    ]
    assert commands.index("run-company acme_o2") < commands.index("author-tasks"), (
        "a world delivers the tasks it has before it designs more"
    )
    commands.clear()  # the next pass finds the mined tasks uncalibrated and carries them through
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert {"run-company acme_o3", "run-company acme_o4"} <= set(commands)
    assert driver.paths(folder, driver.calibrations) == [
        folder / "runtime/grades" / t / "calibration.json" for t in driver.tasks(folder)
    ]
    assert driver.paths(folder, driver.controllers) == [
        folder / "runtime/tasks" / t / "CONTROLLER.json" for t in driver.tasks(folder)
    ]
    receipt = driver.load(folder / driver.MINE)
    assert (receipt["target"], receipt["requested"]) == (4, 2)
    assert receipt["existing"] == ["acme_o1", "acme_o2"] and receipt["accepted"] == ["acme_o3", "acme_o4"]
    assert receipt["unused_outlines"] == ["o3", "o4", "o5"] and receipt["rejected"] == []
    assert receipt["prepared"] == {
        "rewrite-briefs": ["acme_o3", "acme_o4"],
        "assign-apps": ["acme_o3", "acme_o4"],
    }
    commands.clear()  # the receipt is the marker: a later pass mines nothing and runs nothing
    assert driver.position(folder, ctx, log) == ("done", 0) and commands == []


def test_a_world_with_no_unused_outlines_writes_the_receipt_and_never_mines_again(
    folder, fake_pipeline, monkeypatch
):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder, designed=("o1", "o2"), published=("o1", "o2"))  # the design is spent
    assert driver.position(folder, ctx, log) == ("done", 0)
    receipt = driver.load(folder / driver.MINE)
    assert (receipt["requested"], receipt["unused_outlines"], receipt["accepted"]) == (0, [], [])
    assert receipt["prepared"] == {"rewrite-briefs": [], "assign-apps": []}
    assert "author-tasks" not in commands  # a target of four does not conjure a fifth outline
    commands.clear()
    assert driver.position(folder, ctx, log) == ("done", 0) and commands == []


def test_mining_does_not_re_seed_the_accepted_world(folder, fake_pipeline, monkeypatch):
    """The regression this stage exists to avoid: REWRITE.json is the apps stage's input and
    ASSIGN.json the seed stage's, so re-dating either would throw the accepted world away."""
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    chain = [
        folder / p
        for p in ("company.json", driver.REWRITE, driver.ASSIGN, "world/SEED.json", "world/BULK.json")
    ]
    before = [p.stat().st_mtime for p in chain]
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert "seed-world" not in commands
    assert not {"export-company", "rewrite-briefs", "assign-apps", "dedupe-names", "add-bulk"} & set(commands)
    assert [p.stat().st_mtime for p in chain] == before  # not one marker of the chain was re-dated
    assert (folder / driver.MINE).stat().st_mtime > max(before)  # though the receipt is newer than all
    for stage in ("export", "rewrite", "apps", "seed"):
        markers = [m for s, m, *_ in driver.STEPS if s == stage and m is not None]
        assert all(
            driver.fresh(path, driver.paths(folder, driver.INPUTS[stage]))
            for marker in markers
            for path in driver.paths(folder, marker)
        ), stage
    commands.clear()
    # The next pass carries the mined tasks through the finishing stages, and still never re-seeds.
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert "seed-world" not in commands and "author-tasks" not in commands
    assert [p.stat().st_mtime for p in chain] == before


def test_a_rejected_world_is_never_mined(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    put(folder / "world/SEED.json", {"status": "seeded_review_failed"}, age=13, base=time.time() - 3600)
    # The world gets its one repair attempt; it stays rejected, so nothing is ever mined from it.
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == ["review-repair"] and not (folder / driver.MINE).exists()
    commands.clear()
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == [] and not (folder / driver.MINE).exists()  # the receipt stops a second try
    assert driver.position(folder) == ("seed", "review_not_accepted")


def test_the_receipt_makes_the_readability_gate_stale_and_nothing_before_it(
    folder, fake_pipeline, monkeypatch
):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    seeded(folder)  # a company checked long before it was mined
    put(folder / driver.MINE, {"mined_at": "now"}, age=100)  # ... and mined now
    readability, checks = folder / "REPORT-readability.json", folder / "world/CHECKS.json"
    assert not driver.fresh(readability, driver.paths(folder, driver.INPUTS["readability"]))
    assert driver.fresh(checks, driver.paths(folder, driver.INPUTS["check"]))
    for stage in ("export", "rewrite", "apps", "seed", "check", "calibrate"):
        assert folder / driver.MINE not in driver.paths(folder, driver.INPUTS[stage]), stage
    assert driver.position(folder) == ("readability", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    # The new briefs are read again; the world itself did not change, so it is not checked again
    # and the calibrations and VM episodes of the tasks that were already proved still stand.
    assert commands[0] == "readability"
    assert not {"seed-world", "world-check", "sync-worker-apps"} & set(commands)


def test_authoring_that_fails_is_retried_and_an_exhausted_design_is_recorded(
    folder, fake_pipeline, monkeypatch
):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands, results={"author-tasks": 1}))
    assert driver.position(folder, ctx, log) == ("mine", "mine_failed")
    assert [c for c in commands if c == "author-tasks"] == ["author-tasks"] and not (
        folder / driver.MINE
    ).exists()
    assert driver.position(folder) == ("mine", "pending")  # nothing recorded: the next loop retries
    # A provider outage says nothing about this world, and is not this stage's failure.
    log.write_text("ModelUnavailable: codex/gpt-6-astra: You've hit your usage limit.\n")
    assert driver.position(folder, ctx, log) == ("mine", "model_unavailable")
    assert not (folder / driver.MINE).exists()
    # author-tasks counting the outlines differently is not a failure either: there is nothing
    # to mine, which is exactly what the receipt is for.
    log.write_text("ValueError: requested 2 tasks but only 0 unused outlines remain\n")
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert driver.load(folder / driver.MINE)["note"] == "author-tasks found no unused outlines"


def test_a_mined_task_the_seeded_world_cannot_staff_stops_the_stage(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    hour_ago = time.time() - 3600
    put(folder / "world/worker_apps.json", {"lead": ["x"]}, age=14, base=hour_ago)  # two hold nothing
    assert driver.position(folder, ctx, log) == ("mine", "mine_unprepared")
    assert commands[-1] == "sync-worker-apps" and not (folder / driver.MINE).exists()
    assert "worker_apps.json does not cover" in log.read_text()
    # The other half of the same gate: a task the plain-brief pass never saw.
    put(folder / "tasks/acme_o5/workflow.json", workflow("o5", prepared=False), age=200)
    assert any("no plain brief" in gap for gap in driver.preparation_gaps(folder))


def test_render_failure_stops_before_calibration_and_blocks_the_vm_stage(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    results = {"render-check": 1}
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands, results=results))
    assert driver.position(folder, ctx, log) == ("render", "render_failed")
    assert commands[-1] == "render-check" and "author-golden" not in commands and "calibrate" not in commands
    # The marker records the failure; inspection sees it, the next loop retries the render, nothing boots.
    put(
        folder / "runtime/RENDER.json",
        {"ok": False, "apps": {"drive_mock": {"ok": False, "loading": True}}},
        age=300,
    )
    assert driver.position(folder) == ("render", "pending")
    commands.clear()
    assert driver.position(folder, ctx, log) == ("render", "render_failed")
    assert commands == ["render-check"] and not (folder / "runtime/tasks/t1/CONTROLLER.json").exists()
    view = driver.status_view([("r1", "acme")], folder.parent, {})["companies"]["acme"]
    assert (view["stage"], view["outcome"]) == ("render", "pending")
    assert view["steps"]["render"]["ok"] is False and view["steps"]["calibrate"]["ok"] is False
    # A host without a browser writes a skipped, ok report: not a failure.
    assert driver.render_ok({"ok": True, "browser": None, "skipped": "no headless browser found", "apps": {}})
    assert not driver.render_ok({}) and not driver.render_ok({"ok": "yes"})
    # Once the render passes, the same served hub goes on to golden, calibration, then the VM stage.
    commands.clear()
    results.clear()
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == [
        "render-check",
        "visual-judge",
        "author-golden",
        "calibrate",
        "run-company t1",
        "review-sheet",
        "agreement",
    ]


def test_render_is_redone_when_a_state_file_or_the_hub_patches_change(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    assert driver.position(folder, ctx, log) == ("done", 0)
    render = folder / "runtime/RENDER.json"
    # A state file rewritten after the render (say by a hub patch's reseed) makes RENDER.json stale.
    # The episode under it is not re-run for that: a re-render recomputes an opinion, and this state
    # file is older than the calibration, which is the input that speaks for the records.
    commands.clear()
    later = render.stat().st_mtime - CLOCK + 0.5  # between the old render and the next command's marker
    put(folder / "world/drive_mock.state.json", {"files": []}, age=later)
    assert driver.position(folder) == ("render", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["render-check", "visual-judge"]  # calibration, episode and sheet stand
    commands.clear()
    later = render.stat().st_mtime - CLOCK + 0.5
    put(driver.HUB_PATCHES, {"patched": True}, age=later)
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["render-check", "visual-judge"]
    commands.clear()
    assert driver.position(folder, ctx, log) == ("done", 0) and commands == []
    # Only a stale calibration does not re-render: the hub is served once and calibrates alone.
    (folder / "runtime/grades/t1/calibration.json").unlink()
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["calibrate", "run-company t1", "review-sheet", "agreement"]


def test_deleting_the_controller_redoes_vm_sheet_and_agreement(folder, fake_pipeline):
    commands, ctx, log = fake_pipeline
    driver.position(folder, ctx, log)
    commands.clear()
    (folder / "runtime/tasks/t1/CONTROLLER.json").unlink()
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["run-company t1", "review-sheet", "agreement"]
    commands.clear()
    # An interrupted run resumes.
    put(folder / "runtime/tasks/t1/CONTROLLER.json", {"status": "failed", "task_id": "t1"}, age=500)
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["run-company t1", "review-sheet", "agreement"]


def test_every_task_gets_its_own_vm_run_and_checkpoint(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    put(folder / "tasks" / "t2" / "workflow.json", {"id": "t2"})
    put(folder / "tasks" / "t0" / "workflow.json", {"id": "t0"})
    assert driver.position(folder, ctx, log) == ("done", 0)
    vm_runs = [c for c in commands if c.startswith("run-company")]
    assert vm_runs == ["run-company t0", "run-company t1", "run-company t2"]  # one call per task, in order
    assert commands.index("run-company t0") > commands.index("calibrate")
    for task in ("t0", "t1", "t2"):
        assert driver.load(folder / "runtime/tasks" / task / "CONTROLLER.json")["status"] == "done"
    assert not (folder / "runtime/CONTROLLER.json").exists()
    assert driver.paths(folder, driver.controllers) == [
        folder / "runtime/tasks" / t / "CONTROLLER.json" for t in ("t0", "t1", "t2")
    ]
    # Only the task whose checkpoint is short of done runs again; the others are left alone.
    commands.clear()
    put(folder / "runtime/tasks/t1/CONTROLLER.json", {"status": "failed", "task_id": "t1"}, age=900)
    assert driver.position(folder) == ("vm", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["run-company t1", "review-sheet", "agreement"]
    # A task that fails does not stop the others; the outcome names every task's exit code.
    commands.clear()
    for task in ("t0", "t2"):
        put(
            folder / "runtime/tasks" / task / "CONTROLLER.json",
            {"status": "failed", "task_id": task},
            age=950,
        )
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands, results={"run-company t0": 3}))
    assert driver.position(folder, ctx, log) == ("vm", "t0=3 t2=0")
    assert commands == ["run-company t0", "run-company t2"]
    assert driver.load(folder / "runtime/tasks/t2/CONTROLLER.json")["status"] == "done"
    assert driver.load(folder / "runtime/tasks/t0/CONTROLLER.json")["status"] == "failed"
    view = driver.status_view([("r1", "acme")], folder.parent, {})["companies"]["acme"]
    assert (view["stage"], view["outcome"]) == ("vm", "pending") and view["steps"]["vm"]["ok"] is False


def test_a_checkpoint_at_the_old_location_is_that_tasks_marker(folder, fake_pipeline):
    commands, ctx, log = fake_pipeline
    put(folder / "tasks" / "t2" / "workflow.json", {"id": "t2"})
    driver.position(folder, ctx, log)
    commands.clear()
    # A company that ran before the per-task layout: one checkpoint at runtime/CONTROLLER.json.
    shutil.move(folder / "runtime/tasks/t1/CONTROLLER.json", folder / "runtime/CONTROLLER.json")
    shutil.rmtree(folder / "runtime/tasks/t1")
    assert driver.controller_state(folder, "t1") == folder / "runtime/CONTROLLER.json"
    assert driver.controller_state(folder, "t2") == folder / "runtime/tasks/t2/CONTROLLER.json"
    assert folder / "runtime/CONTROLLER.json" in driver.agreement_inputs(folder)
    assert driver.position(folder) == ("done", 0)  # t1 is accounted for where it is
    assert driver.position(folder, ctx, log) == ("done", 0) and commands == []
    # The old checkpoint naming a task that is not t1 says nothing about t1.
    put(folder / "runtime/CONTROLLER.json", {"status": "done", "task_id": "t9"}, age=990)
    assert driver.position(folder) == ("vm", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands == ["run-company t1", "review-sheet", "agreement"]


def rejecting_cli(folder, commands, saved):
    """fake_cli whose seed-world is a world the reviewers rejected, and whose review-repair saves
    it (SAVED) or does not; both write the receipt the driver reads."""
    base = fake_cli(folder, commands)
    markers = {"seed-world": "world/SEED.json", "review-repair": "world/REVIEW-REPAIR.json"}

    def run(cmd, log, timeout):
        rc = base(cmd, log, timeout)
        verdict = {
            "seed-world": {"status": "seeded_review_failed", "review_verdict": "revise"},
            "review-repair": {"status": "seeded_reviewed", "review_verdict": "accept"} if saved else None,
        }.get(cmd[3])
        if verdict:  # the verdict lands just after the receipt its command wrote, as on a real disk
            age = (folder / markers[cmd[3]]).stat().st_mtime - CLOCK + 0.5
            put(folder / "world/SEED.json", verdict, age=age)
        return rc

    return run


def test_a_rejected_world_is_repaired_once_and_goes_on_when_that_works(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    monkeypatch.setattr(driver, "run", rejecting_cli(folder, commands, saved=True))
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands[3:6] == ["seed-world", "review-repair", "dedupe-names"]
    assert commands.count("seed-world") == 1 and commands.count("review-repair") == 1
    assert driver.load(folder / "world/REVIEW-REPAIR.json")["rounds"] == 1
    commands.clear()
    assert driver.position(folder, ctx, log) == ("done", 0) and commands == []


def test_a_world_the_repair_cannot_save_stays_terminal_and_is_not_retried(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    monkeypatch.setattr(driver, "run", rejecting_cli(folder, commands, saved=False))
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == ["export-company", "rewrite-briefs", "assign-apps", "seed-world", "review-repair"]
    assert "review_not_accepted" in driver.TERMINAL
    # The receipt is the memory: the next loop reads the verdict and runs nothing at all.
    commands.clear()
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == []
    assert driver.position(folder) == ("seed", "review_not_accepted")  # inspection never repairs
    assert commands == []


def test_a_failing_command_stops_the_company_at_its_stage(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands, results={"world-check": 2}))
    assert driver.position(folder, ctx, log) == ("check", 2)
    # A failing check gets one repair and one more check, never a third.
    assert commands[-3:] == ["world-check", "world-repair", "world-check"]
    put(folder / "world/CHECKS.json", {"ok": False, "errors": ["x"], **stamped("world_check")}, age=200)
    assert driver.position(folder) == ("check", "check_failed")
    commands.clear()
    assert driver.position(folder, ctx, log) == ("check", "check_failed")
    assert commands == []  # the marker is fresh: nothing reruns until an input of the stage changes


def test_a_failing_check_is_repaired_once_and_checked_again(folder, fake_pipeline, monkeypatch):
    commands, ctx, log = fake_pipeline
    monkeypatch.setattr(driver, "run", fake_cli(folder, commands, results={"world-check": [2, 0]}))
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands[8:15] == [
        "world-check", "world-repair", "world-check", "render-check", "visual-judge",
        "author-golden", "calibrate",
    ]  # fmt: skip
    assert commands.count("world-repair") == 1
    assert (folder / "world/REPAIR.json").is_file() and driver.load(folder / "world/CHECKS.json")["ok"]


def test_lock_takeover_from_dead_pid(tmp_path):
    locks = tmp_path / "locks"
    dead = subprocess.Popen(["true"])
    dead.wait()
    put(locks / "acme", None)
    (locks / "acme").write_text(str(dead.pid))
    assert driver.claim("acme", locks)
    assert (locks / "acme").read_text() == str(os.getpid())
    assert driver.claim("acme", locks)  # our own lock is fine to take again
    live = subprocess.Popen(["sleep", "30"])
    try:
        (locks / "other").write_text(str(live.pid))
        assert not driver.claim("other", locks)
    finally:
        live.kill()
        live.wait()
    (locks / "junk").write_text("not a pid")
    assert driver.claim("junk", locks)
    driver.release(["acme", "junk", "other"], locks)
    assert sorted(p.name for p in locks.iterdir()) == ["other"]


def test_status_view_is_derived_from_artifacts(folder, tmp_path):
    seeded(folder, status="seeded_review_failed")
    view = driver.status_view([("r1", "acme"), ("r1", "nobody")], folder.parent, {"acme": {"stage": "seed"}})
    acme = view["companies"]["acme"]
    assert (acme["run"], acme["stage"], acme["outcome"]) == ("r1", "seed", "review_not_accepted")
    assert acme["steps"]["export"]["ok"] and acme["steps"]["export"]["at"].startswith("2027-")
    assert not acme["steps"]["seed"]["ok"] and "completed_at" not in acme
    assert acme["last"] == {"stage": "seed"}
    nobody = view["companies"]["nobody"]
    assert (nobody["stage"], nobody["outcome"]) == ("export", "pending")
    assert all(not step["ok"] and step["at"] is None for step in nobody["steps"].values())
    put(folder / "AGREEMENT.json", {"done": True, "checked_at": "then"})
    assert (
        driver.status_view([("r1", "acme")], folder.parent, {})["companies"]["acme"]["completed_at"] == "then"
    )
    driver.write_json(tmp_path / "batch" / "STATUS.json", view)
    assert json.loads((tmp_path / "batch" / "STATUS.json").read_text()) == view
    assert list((tmp_path / "batch").iterdir()) == [tmp_path / "batch" / "STATUS.json"]


def test_guest_slots_are_acquired_by_roster_size(tmp_path, folder, monkeypatch):
    slots = tmp_path / "vm-slots"
    five = driver.vm_slot(5, 12, slots)
    assert five is not None and len(five) == 5
    assert sorted(Path(h.name).name for h in five) == [f"{i}.lock" for i in range(5)]
    six = driver.vm_slot(6, 12, slots)
    assert six is not None and len(six) == 6
    assert driver.vm_slot(2, 12, slots) is None  # one guest left of twelve: not enough, none taken
    two = driver.vm_slot(1, 12, slots)
    assert two is not None and Path(two[0].name).name == "11.lock"
    for handle in five:
        handle.close()  # closing the handles releases the guests
    assert len(driver.vm_slot(5, 12, slots)) == 5

    put(folder / "tasks/t1/workflow.json", {"manager_id": "lead", "worker_ids": ["lead", "a", "b", "c"]})
    assert driver.roster(folder, "t1") == ["lead", "a", "b", "c"]  # the manager counts once
    assert driver.guest_budget({"vm_slots": 4}) == 20  # the legacy unit: a slot was one company of five
    assert driver.guest_budget({}) == 20
    assert driver.guest_budget({"vm_slots": 4, "vm_guests": 12}) == 12  # vm_guests says it directly

    design = {"vm_base_image": "img", "vm_browser_dir": "dir", "vm_host_ip": "10.0.2.2", "vm_slots": 2}
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": design})
    asked = []
    monkeypatch.setattr(
        driver, "vm_slot", lambda guests, budget, slot_dir=None: asked.append((guests, budget))
    )
    assert driver.vm({}, folder, tmp_path / "log") == "vm_slots_busy"
    assert asked == [(4, 10)]  # the roster's four guests out of two legacy slots' worth
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": {**design, "vm_guests": 3}})
    assert driver.vm({}, folder, tmp_path / "log") == "vm_over_budget"
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": {}})
    assert driver.vm({}, folder, tmp_path / "log") == "vm_unconfigured"


def test_vm_holds_guests_while_run_company_runs_and_releases_after(tmp_path, folder, monkeypatch):
    put(folder / "tasks/t1/workflow.json", {"manager_id": "lead", "worker_ids": ["a", "b"]})
    design = {"vm_base_image": "img", "vm_browser_dir": "dir", "vm_host_ip": "10.0.2.2", "vm_guests": 4}
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": design})
    slots, real_slot, seen = tmp_path / "slots", driver.vm_slot, {}
    monkeypatch.setattr(
        driver, "vm_slot", lambda guests, budget, slot_dir=None: real_slot(guests, budget, slots)
    )

    def run(cmd, log, timeout):
        seen["guests"] = dict(driver.GUESTS)
        seen["free"] = real_slot(1, 4, slots)  # only one guest of four is free during the run
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(driver, "run", run)
    assert driver.vm({}, folder, tmp_path / "log") == 0
    assert seen["guests"] == {"acme": 3} and seen["free"] is not None
    assert driver.GUESTS == {}
    assert real_slot(4, 4, slots) is None  # seen["free"] still holds the fourth guest
    three = real_slot(3, 4, slots)
    assert three is not None  # the run's three guests are back
    for handle in (*three, *seen["free"]):
        handle.close()
    assert len(real_slot(4, 4, slots)) == 4
    assert seen["cmd"][3:6] == ["run-company", str(folder), "t1"]


def test_vm_waits_for_an_orphan_run_company_instead_of_starting_a_second(tmp_path, folder, monkeypatch):
    put(folder / "tasks/t1/workflow.json", {"manager_id": "lead", "worker_ids": ["a"]})
    design = {"vm_base_image": "img", "vm_browser_dir": "dir", "vm_host_ip": "10.0.2.2", "vm_guests": 4}
    monkeypatch.setattr(driver, "load_toml", lambda path: {"design": design})
    monkeypatch.setattr(driver, "running_elsewhere", lambda folder, task: task == "t1")
    monkeypatch.setattr(driver, "run", lambda cmd, log, timeout: pytest.fail("started a second run"))
    assert driver.vm({}, folder, tmp_path / "log") == "t1=running_elsewhere"
    assert driver.GUESTS == {}


def test_review_verdict_and_later_calibration_override_stale_markers(folder):
    import json
    import os
    import time

    (folder / "world").mkdir(parents=True, exist_ok=True)
    (folder / "world/SEED.json").write_text(json.dumps({"status": "seeded_review_failed"}))
    (folder / "world/REVIEW.json").write_text(json.dumps({"rounds": [{"verdict": {"verdict": "accept"}}]}))
    assert (
        driver.review_accepted(None, folder, None) == 0
    )  # the reviewers' verdict wins over the folded status
    (folder / "world/REVIEW.json").write_text(json.dumps({"rounds": [{"verdict": {"verdict": "revise"}}]}))
    assert driver.review_accepted(None, folder, None) == "review_not_accepted"
    # A newer seed records the verdict on its own; that field wins over both status and REVIEW.json.
    (folder / "world/SEED.json").write_text(
        json.dumps({"status": "seeded_review_failed", "review_verdict": "accept", "mechanical_ok": False})
    )
    assert driver.review_accepted(None, folder, None) == 0
    (folder / "world/SEED.json").write_text(
        json.dumps({"status": "seeded_reviewed", "review_verdict": "revise"})
    )
    (folder / "world/REVIEW.json").write_text(json.dumps({"rounds": [{"verdict": {"verdict": "accept"}}]}))
    assert driver.review_accepted(None, folder, None) == "review_not_accepted"
    (folder / "world/SEED.json").write_text(json.dumps({"status": "seeded_review_failed"}))

    task = folder / "tasks" / "t1"
    task.mkdir(parents=True, exist_ok=True)
    (task / "workflow.json").write_text("{}")
    failed = task / "CALIBRATION_FAILED.json"
    failed.write_text("{}")
    assert driver.not_rejected(None, folder, None) == "calibration_rejected"
    proof = folder / "runtime" / "grades" / "t1" / "calibration.json"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({"accepted": True, "scope": "all_checks"}))
    old = time.time() - 100
    os.utime(failed, (old, old))
    assert driver.not_rejected(None, folder, None) == 0  # a later accepted calibration supersedes the failure


def test_a_model_outage_does_not_count_as_a_seed_failure(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("$ seed-world ...\nModelUnavailable: codex/gpt-6-astra: You've hit your usage limit.\n")
    assert driver.outage(log) is True
    log.write_text(
        "$ seed-world ...\nValueError: identity mismatch: broker-lead is not in any state collection\n"
    )
    assert driver.outage(log) is False


def test_work_copy_is_pruned_at_done_or_a_terminal_outcome_only(folder, tmp_path):
    ctx, log = {"dir": tmp_path / "batch", "work": tmp_path / "cache"}, tmp_path / "batch" / "acme.log"
    work = ctx["dir"] / "work" / "acme"
    for stage, outcome in (("seed", "pending"), ("vm", "vm_slots_busy"), ("seed", "model_unavailable")):
        put(work / "gmail_mock" / "dist" / "index.html")
        driver.housekeep(ctx, folder, log, stage, outcome)
        assert work.is_dir()  # still needed
    for stage, outcome in (
        ("seed", "review_not_accepted"),
        ("check", "check_failed"),
        ("calibrate", "calibration_rejected"),
        ("seed", "seed_failed_repeatedly"),
        ("done", 0),
    ):
        put(work / "gmail_mock" / "dist" / "index.html")
        driver.housekeep(ctx, folder, log, stage, outcome)
        assert not work.exists(), (stage, outcome)
        driver.housekeep(ctx, folder, log, stage, outcome)  # nothing left to do: idempotent, silent
    notes = [line for line in log.read_text().splitlines() if line.startswith("pruned work copy")]
    assert len(notes) == 5 and notes[0].endswith("+00:00]") and str(work) in notes[0]


def test_set_aside_controller_runtimes_keep_only_evidence(folder, tmp_path):
    runtime = folder / "runtime"
    live = runtime / "controller" / "company" / "runtime" / "teacher" / "t1" / "TEACHER.json"
    put(live, {"live": True})
    put(runtime / "controller" / "npm-cache" / "big.bin")
    put(runtime / "CONTROLLER.json", {"status": "running"})
    put(runtime / "CONTROLLER.quota-1.json", {"status": "failed"})
    archive = runtime / "controller.quota-1"
    kept = {
        "company/runtime/teacher/t1/TEACHER.json": {"passed": False},
        "company/runtime/grades/t1/calibration.json": {"accepted": True},
        "company/runtime/grades/t1/report.json": {},
        "company/runtime/grades/t1/judge_bench.json": {},
        "company/runtime/vms/lead/trace/events.jsonl": None,
        "company/runtime/episode-result.json": {},
        "company/CONTROLLER.json": {"status": "failed"},
        "CONTROLLER.snapshot.json": {},
    }
    dropped = ["npm-cache/_cacache/x", "company/world/SEED.json", "company/runtime/vms/lead/disk.qcow2", ".lock", "models/calls/1.json"]  # fmt: skip
    for rel, data in kept.items():
        put(archive / rel, data)
    for rel in dropped:
        put(archive / rel)
    (archive / "link").symlink_to(archive / "company")
    log = tmp_path / "acme.log"
    assert driver.prune_archives(folder, log) == ["controller.quota-1"]
    assert [p.name for p in archive.iterdir()] == ["EVIDENCE"]
    found = sorted(
        str(p.relative_to(archive / "EVIDENCE")) for p in (archive / "EVIDENCE").rglob("*") if p.is_file()
    )
    assert found == sorted(kept)
    assert json.loads((archive / "EVIDENCE/company/runtime/grades/t1/calibration.json").read_text()) == {"accepted": True}  # fmt: skip
    assert (
        live.is_file() and (runtime / "controller/npm-cache/big.bin").is_file()
    )  # the live runtime is untouched
    assert (runtime / "CONTROLLER.quota-1.json").is_file()
    assert driver.prune_archives(folder, log) == []  # already pruned: nothing happens, nothing is logged
    assert log.read_text().count("pruned ") == 1 and "(8 files kept)" in log.read_text()
    # An archive pruned by hand (EVIDENCE/ plus leftovers) is finished the same way.
    put(archive / "npm-cache/late.bin")
    put(archive / "company/runtime/teacher/t1/TEACHER.json", {"passed": True})  # a duplicate never overwrites
    assert driver.prune_archives(folder, log) == ["controller.quota-1"]
    assert [p.name for p in archive.iterdir()] == ["EVIDENCE"]
    assert json.loads((archive / "EVIDENCE/company/runtime/teacher/t1/TEACHER.json").read_text()) == {"passed": False}  # fmt: skip


def test_world_archives_go_after_seven_days_unless_kept(folder, tmp_path):
    put(folder / "world/SEED.json", {})
    for name in ("world.old-instructions-20260908", "world.rejected-20260907", "world.keep"):
        put(folder / name / "world.json", {})
    log = tmp_path / "acme.log"
    assert driver.prune_worlds(folder, log) == []  # set aside just now
    later = lambda: driver.time.time() + 8 * 86400
    assert driver.prune_worlds(folder, log, clock=later) == ["world.old-instructions-20260908", "world.rejected-20260907"]  # fmt: skip
    assert sorted(p.name for p in folder.iterdir()) == [
        "tasks",
        "world",
        "world.keep",
    ]  # world/ itself is never touched
    assert log.read_text().count("removed world archive") == 2
    put(folder / "world.rejected-20260901" / "world.json", {})
    ctx = {"dir": tmp_path / "batch", "work": tmp_path / "cache"}
    driver.housekeep(ctx, folder, log, "seed", "pending", keep_archives=True)
    assert (folder / "world.rejected-20260901").is_dir()  # --keep-archives
    driver.housekeep(ctx, folder, log, "seed", "pending")
    assert (folder / "world.rejected-20260901").is_dir()  # not old enough either way


def test_health_line_counts_companies_and_rotates_the_file(folder, tmp_path, monkeypatch):
    seeded(folder, status="seeded_review_failed")
    put(folder.parent / "globex" / "tasks" / "t1" / "workflow.json", {})
    view = driver.status_view([("r1", "acme"), ("r1", "globex")], folder.parent, {})
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "acme.log").write_text("ModelUnavailable: You've hit your usage limit.\nhit your usage limit\n")
    (batch / "old.log").write_text("hit your usage limit\n")
    stale = driver.time.time() - 2 * driver.HOUR
    os.utime(batch / "old.log", (stale, stale))  # written before the last hour
    monkeypatch.setattr(driver, "own_seeds", lambda: 2)
    monkeypatch.setitem(driver.GUESTS, "acme", 5)
    line = driver.health_line(view, batch)
    assert line["companies"] == 2 and line["stages"] == {"export": 1, "seed": 1}
    assert line["outcomes"] == {"pending": 1, "review_not_accepted": 1}
    assert (line["seeds_running"], line["guests_held"], line["usage_limit_hits_last_hour"]) == (2, 5, 2)
    assert line["disk_free_gb"] > 0 and line["pid"] == os.getpid() and line["time"].endswith("+00:00")
    path = batch / "health.jsonl"
    driver.append_health(path, line, cap=400)
    driver.append_health(path, line, cap=400)
    assert len(path.read_text().splitlines()) == 2
    driver.append_health(path, line, cap=400)  # over the cap: the file rotates to health.jsonl.1
    assert len(path.read_text().splitlines()) == 1
    assert len((batch / "health.jsonl.1").read_text().splitlines()) == 2
    assert json.loads(path.read_text()) == line


def test_calibrate_waits_for_an_orphan_calibration_instead_of_racing_it(tmp_path, folder, monkeypatch):
    monkeypatch.setattr(driver, "calibrating_elsewhere", lambda folder: True)
    monkeypatch.setattr(driver.subprocess, "Popen", lambda *a, **k: pytest.fail("served a second hub"))
    assert driver.calibrate({}, folder, tmp_path / "log") == "calibrating_elsewhere"


def test_a_change_to_the_serving_code_makes_every_render_stale(folder, monkeypatch, tmp_path):
    """A proxy fix changes every served page while no state file moves; the render must redo."""
    seeded(folder)
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=50)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=51)
    put(folder / "runtime/grades/t1/calibration.json", {"accepted": True, "scope": "all_checks"}, age=52)
    assert driver.position(folder) == ("vm", "pending")
    proxy = put(tmp_path / "hub_identity.py", {}, age=60)  # the serving code changes after the render
    monkeypatch.setattr(driver, "PROXY_CODE", (proxy,))
    assert driver.position(folder) == ("render", "pending")


def test_slots_go_to_the_companies_nearest_to_delivered(folder, tmp_path):
    """Ninety seeds must not crowd out a dozen worlds that only need the finishing stages."""
    root = folder.parent
    fresh = root / "fresh"
    put(fresh / "company.json", {})
    seeded_world = root / "seeded"
    put(seeded_world / "company.json", {})
    put(seeded_world / "world/SEED.json", {"status": "seeding"})
    accepted = root / "accepted"
    put(accepted / "company.json", {})
    put(accepted / "world/SEED.json", {"status": "seeded_reviewed"})
    calibrated = root / "calibrated"
    put(calibrated / "company.json", {})
    put(calibrated / "world/SEED.json", {"status": "seeded_reviewed"})
    put(calibrated / "runtime/grades/t1/calibration.json", {"accepted": True})
    delivered = root / "delivered"
    put(delivered / "AGREEMENT.json", {"done": True})

    ranked = sorted(
        ["fresh", "seeded", "accepted", "calibrated", "delivered", "missing"],
        key=lambda name: -driver.progress(root / name),
    )
    assert ranked == ["delivered", "calibrated", "accepted", "seeded", "fresh", "missing"]


def test_a_world_whose_spare_outlines_are_refused_is_mined_only_once(folder, fake_pipeline, monkeypatch):
    """Retrying holds a slot on the companies furthest along, which are the ones left to deliver."""
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    real = fake_cli(folder, commands)

    def refusing(cmd, log_path, timeout):
        if "author-tasks" in cmd:  # the designer read the world and would not build on what is left
            commands.append("author-tasks")
            with open(log_path, "a") as stream:
                stream.write(f"ModelOutputInvalid: {driver.DECLINED}\n")
            return 1
        return real(cmd, log_path, timeout)

    monkeypatch.setattr(driver, "run", refusing)
    driver.position(folder, ctx, log)
    assert commands.count("author-tasks") == 1
    receipt = driver.load(folder / driver.MINE)
    assert receipt["accepted"] == [] and "declined" in receipt["note"]
    commands.clear()
    driver.position(folder, ctx, log)
    assert "author-tasks" not in commands, "the receipt stops a second attempt"


def test_an_over_ceiling_mining_payload_is_recorded_and_asked_for_once(folder, fake_pipeline, monkeypatch):
    """author-tasks stopped raising PromptTooLarge and returns a report with nothing accepted, which is
    what ends the retry: raising had the step rebuild the same payload every loop, 12 call ids across
    12 companies reaching 234 attempts that could never succeed. Verified here rather than trusted: the
    command exits 0, mine() returns 0, MINE.json is written once and mined_ok stops a second pass.

    What exit 0 with no task does not say is why, and a designer that simply accepted nothing looks
    identical -- the other two ways to mine nothing each write a note. The refusal leaves its own
    receipt beside the rejected drafts, so the reason comes from that, and the loop stops asking for
    more of something the refusal itself calls unchangeable by retrying."""
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    real = fake_cli(folder, commands)
    reason = "design: world_design prompt is 900001 characters. ... no number of retries changes it."

    def refusing(cmd, log_path, timeout):
        if "author-tasks" in cmd:  # exactly what oversized_mining leaves: a receipt, a warning, rc 0
            commands.append("author-tasks")
            diagnostic = folder / "tasks/_rejected/oversized-abc123.json"
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            diagnostic.write_text(json.dumps({"at": "now", "call": "design", "reason": reason}))
            with open(log_path, "a") as stream:
                stream.write(f"warning: author-tasks mined nothing: {reason}\n")
            return 0
        return real(cmd, log_path, timeout)

    monkeypatch.setattr(driver, "run", refusing)
    assert driver.position(folder, ctx, log) == ("done", 0), "the world keeps the tasks it published"
    assert commands.count("author-tasks") == 1, "a refusal no retry can change is asked for once"
    receipt = driver.load(folder / driver.MINE)
    assert receipt["accepted"] == [] and receipt["requested"] == 2
    assert reason in receipt["note"] and "over-ceiling" in receipt["note"]
    # And the shared word from company_envs.receipt, so the three ways to mine nothing are no longer
    # one shape: this is a verdict no retry can change, which is not what a world already at target
    # records. `mined_at` still decides whether the step re-runs.
    assert receipt["outcome"] == "refused" and reason in receipt["reason"]
    assert driver.mined_ok(receipt) and driver.rejected_tasks(folder) == []
    commands.clear()
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert "author-tasks" not in commands, "the receipt stops a second attempt"
    # A diagnostic left by an earlier run is not this run's answer.
    assert driver.oversized_refusal(folder, time.time() + 1) is None


def test_a_world_that_failed_its_check_before_the_repair_existed_still_gets_one(
    folder, fake_pipeline, monkeypatch
):
    """The repair lives in a step that only runs when the marker is stale, so a world sitting on an
    old failing check could never reach it."""
    commands, ctx, log = fake_pipeline
    seeded(folder, ok=False)  # a check that failed, with no repair receipt beside it
    put(folder / "world/CHECKS.json", {"ok": False, **stamped("world_check")}, age=40)
    assert driver.position(folder, ctx, log) == ("check", "check_failed")
    assert commands == ["world-repair"], "the gate gives it the attempt the step never could"
    commands.clear()
    assert driver.position(folder, ctx, log) == ("check", "check_failed")
    assert commands == [], "the receipt keeps it to one attempt"


def test_a_world_delivers_its_own_tasks_before_it_mines_more(folder, fake_pipeline, monkeypatch):
    """Mining ahead of the finishing stages made accepted worlds wait on a task design before they
    could render, calibrate or reach a VM."""
    commands, ctx, log = fake_pipeline
    with_target(monkeypatch, 4)
    mineable(folder)
    assert driver.position(folder, ctx, log) == ("done", 0)
    for earlier in ("world-check", "render-check", "visual-judge", "calibrate", "agreement"):
        assert commands.index(earlier) < commands.index("author-tasks"), (
            f"{earlier} must not wait on a task design"
        )


def test_a_checker_change_makes_every_check_stale_and_recalibrates_nothing(
    folder, fake_pipeline, monkeypatch, tmp_path
):
    """world_check.py changed four times on 2026-09-09 and not one of the 60 CHECKS.json moved.

    INPUTS["check"] was only the bulk marker, so every mechanical verdict in the cohort was the old
    checker's -- 100% of the markers on disk predated every fix of that night. The verdict is cheap
    to retake, so the checker is an input. The proof under it is not: re-dating 60 calibrations and
    the VM episodes under them because someone edited the checker is hours of model and guest time
    spent re-proving worlds whose records never moved.

    Nor is the stage's other marker. world/worker_apps.json is written by sync-worker-apps out of
    state_seed.py, and while the code was listed per stage an edit to the checker re-ran 60 of those
    syncs for nothing. deciding_code() is per marker for exactly this.
    """
    commands, ctx, log = fake_pipeline
    assert driver.position(folder, ctx, log) == ("done", 0)
    commands.clear()
    checker = put(tmp_path / "checker.py", None, age=10_000)  # a fix lands after every marker
    monkeypatch.setattr(driver, "CHECK_CODE", (checker,))
    assert driver.position(folder) == ("check", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    # The world is checked again and the agreement re-read, because both are minutes of CPU. The
    # worker-app sync, the calibrations and the VM episode under them are not touched: no record moved.
    assert commands == ["world-check", "review-sheet", "agreement"]
    for marker in ("world/SEED.json", "world/worker_apps.json", driver.calibrations, driver.controllers):
        stage = next(s for s, m, *_ in driver.STEPS if m == marker)
        assert checker not in driver.needed(folder, stage, marker), marker


def test_the_hub_that_serves_a_page_is_a_render_input(folder):
    """The quota fix and the write-success amendment live in hub_app.py, and it was not listed: the
    20 RENDER.json and 12 VISUAL.json on disk judged pages served before either, with 76 of 88 apps
    silently showing demo data instead of the seeded world."""
    hub = driver.WORLD_SRC / "hub_app.py"
    assert hub in driver.PROXY_CODE
    assert hub in driver.needed(folder, "render", driver.RENDER)
    assert hub in driver.needed(folder, "visual", driver.VISUAL)


def test_a_review_repair_that_ran_no_rounds_is_not_the_attempt_it_is_allowed(
    folder, fake_pipeline, monkeypatch, tmp_path
):
    """Six worlds are terminal on a review repair that ran zero rounds: three on an AttributeError
    already fixed in world_repair.py, three on a codex call that timed out after 1800 s. The gate
    returned as soon as the receipt existed, whatever it said."""
    commands, ctx, log = fake_pipeline
    code = put(tmp_path / "world_repair.py", None, age=50)
    monkeypatch.setattr(driver, "REPAIR_CODE", (code,))
    seeded(folder, status="seeded_review_failed")
    empty = {"rounds": 0, "verdict": None, "error": "ModelUnavailable: ... timed out after 1800 seconds"}
    put(folder / "world/REVIEW-REPAIR.json", empty, age=20)  # written before the fix
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == ["review-repair"], "an empty run is forgiven once, when the code behind it changed"
    commands.clear()
    put(folder / "world/REVIEW-REPAIR.json", empty, age=60)  # ... and empty again, now after the fix
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == [], "a second empty run is final until someone changes the code again"
    put(folder / "world/REVIEW-REPAIR.json", {"rounds": 2, "verdict": "revise"}, age=20)
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert commands == [], "a repair the reviewers read stands however old it is"


def test_a_world_repair_that_made_no_call_is_not_the_attempt_it_is_allowed(
    folder, fake_pipeline, monkeypatch, tmp_path
):
    """macerich's REPAIR.json records one round and no receipts: the only prompt it built was
    3,859,614 characters against a 1,048,576 ceiling and was never sent. rounds is not the test."""
    commands, ctx, log = fake_pipeline
    code = put(tmp_path / "world_repair.py", None, age=50)
    monkeypatch.setattr(driver, "REPAIR_CODE", (code,))
    seeded(folder, ok=False)
    empty = {"rounds": 1, "receipts": {"world": [], "apps": {}}, "error": "prompt is 3,859,614 characters"}
    put(folder / "world/REPAIR.json", empty, age=20)
    put(folder / "world/CHECKS.json", {"ok": False, **stamped("world_check")}, age=21)
    assert driver.position(folder, ctx, log) == ("check", "check_failed")
    assert commands == ["world-repair"], "no call was made, so no attempt was spent"
    assert driver.world_repair_attempted({"receipts": {"apps": {"gmail_mock": [{"job": "x"}]}}})
    assert not driver.world_repair_attempted({"rounds": 2})


def test_a_calibration_round_lost_to_a_hub_that_was_not_serving_does_not_retire_the_task(
    folder, monkeypatch, tmp_path
):
    """matthews' third and final attempt was "golden replay: <urlopen error [Errno 111] Connection
    refused>" -- the hub was not up, so the round was never spent on the task, and the marker
    retired the company's only remaining task anyway."""
    serve = put(tmp_path / "hub_app.py", None, age=50)
    monkeypatch.setattr(driver, "SERVE_CODE", (serve,))
    seeded(folder)
    marker = folder / "tasks/t1/CALIBRATION_FAILED.json"
    fault = "golden replay: <urlopen error [Errno 111] Connection refused>"
    put(marker, {"attempts": ["grader rejected: ...", fault]}, age=20)  # written before the hub fix
    assert driver.tasks(folder) == ["t1"] and driver.not_rejected(None, folder, None) == 0
    put(marker, {"attempts": ["grader rejected: ...", fault]}, age=60)  # and again, now after it
    assert driver.tasks(folder) == [] and driver.not_rejected(None, folder, None) == "calibration_rejected"
    put(marker, {"attempts": ["grader rejected: a check no golden can pass"]}, age=20)
    assert driver.tasks(folder) == [], "a verdict from the grader retires the task however old it is"


def test_a_golden_that_could_not_be_authored_retires_one_task_and_not_the_company(folder):
    """author-golden raised and wrote nothing, and RETIRED did not name it, so rainbow-shops -- an
    accepted all-checks calibration on one of its two tasks -- was held out of the VM stage for ever
    by the other task's golden author returning 'p31' where an object was required."""
    seeded(folder)
    put(folder / "tasks/t2/workflow.json", {"id": "t2"})
    assert "GOLDEN_FAILED.json" in driver.RETIRED
    put(folder / "tasks/t2/GOLDEN_FAILED.json", {"reason": "'p31' is not of type 'object'"})
    assert driver.tasks(folder) == ["t1"], "the company keeps the task whose golden was written"
    assert driver.not_rejected(None, folder, None) == 0
    put(folder / "tasks/t1/GOLDEN_FAILED.json", {"reason": "'p31' is not of type 'object'"})
    assert driver.not_rejected(None, folder, None) == "golden_not_authored"
    assert "golden_not_authored" in driver.TERMINAL, "a terminal outcome prunes the work copy"
    put(folder / "tasks/t2/golden.json", [{"step": 1}])  # a later golden revives the task
    assert driver.tasks(folder) == ["t2"]


def test_a_per_call_timeout_is_this_call_s_answer_and_not_a_provider_outage(tmp_path):
    """models.py wraps a codex call that outran its 1800/3600 s budget in the same ModelUnavailable
    a real outage raises, and "ModelUnavailable" was an outage sign, so the seed step wrote no
    receipt at all. Eleven companies held 2,800 seeding call directories with neither SEED.json nor
    SEED_FAILED.json: national-jewish-health 536, sprouts-farmers-market 521, the-home-depot 519."""
    log = tmp_path / "c.log"
    timeout = (
        '  File "models.py", line 696, in complete\n    raise ModelUnavailable(\n'
        "company_envs.models.ModelUnavailable: codex/gpt-6-astra: Command '['codex', '-a', 'never', "
        "'exec', '--ignore-user-config']' timed out after 1800 seconds\n"
    )
    log.write_text(f"$ seed-world ...\n{timeout}")
    assert driver.outage(log) is False, "a call that ran out of time is a failure with a reason"
    log.write_text(f"$ seed-world ...\n{timeout}\nModelUnavailable: provider process failed: 503\n")
    assert driver.outage(log) is True, "a provider that stopped answering still says nothing about the world"
    log.write_text("$ seed-world ...\ncompany_envs.models.ModelUnavailable: codex/x: stream disconnected\n")
    assert driver.outage(log) is True


def test_a_failed_seed_records_why_and_which_call_failed(folder, monkeypatch, tmp_path):
    """All 37 SEED_FAILED.json on disk are the bare {at, count, rc}: nothing on disk said whether a
    world failed on an identity mismatch, a refused prompt or a provider that stopped answering,
    though the step already reads the tail of the log to decide it was not an outage."""
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    # An error receipt from a run an hour ago: the step started after it, so it is not its failure.
    put(folder / "world/calls/old/attempt-001/receipt.json", {"status": "error"}, base=time.time() - 3600)
    put(folder / "world/calls/abc123/attempt-001/receipt.json", {"status": "complete", "job": "world_seed"})
    put(folder / "world/calls/def456/attempt-003/receipt.json", {"status": "error", "job": "world_states"})
    log = tmp_path / "acme.log"
    reason = "ValueError: identity mismatch: community-manager's identity username differs from user record"

    def failing_seed(cmd, log, timeout):
        log.write_text(f'seeding {cmd[4]}\n  File "state_seed.py", line 897\n{reason}\n')
        return 1

    monkeypatch.setattr(driver, "run", failing_seed)
    monkeypatch.setattr(driver, "seeding_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "prepare_work", lambda ctx, folder, log: tmp_path / "work")
    ctx = {"run": "r1", "dir": tmp_path / "batch", "work": tmp_path / "cache"}
    assert driver.position(folder, ctx, log) == ("seed", 1)
    receipt = driver.load(folder / "world/SEED_FAILED.json")
    assert receipt["count"] == 1 and receipt["reason"] == reason
    assert receipt["call"] == "world/calls/def456/attempt-003" and receipt["job"] == "world_states"
    assert "old" not in receipt["call"], "an error receipt from an earlier run is not this run's failure"
    assert driver.clip("x" * 500).count("…") == 1, "a codex command line keeps both of its ends"


def test_the_seeds_own_call_budget_is_a_reason_the_receipt_can_record(folder, monkeypatch, tmp_path):
    """design.seed_call_budget is cumulative over a world's recorded attempts, and CallBudgetExhausted
    is raised before the call is dispatched, exactly as PromptTooLarge is. It was the one pre-dispatch
    refusal the failure-line pattern did not match, so a company stopped by its budget wrote the bare
    {at, count, rc} three times and never said which wall it hit. Measured 2026-09-10: 23 of the 99
    companies are past 500 cumulative calls and 9 of them hold no world, so this is the next receipt
    those companies write."""
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    log = tmp_path / "acme.log"
    reason = "company_envs.models.CallBudgetExhausted: model call budget exhausted: 900 of 500 spent"

    def refused(cmd, log, timeout):
        log.write_text(f"seeding {cmd[4]}\n{reason}\n")
        return 1

    monkeypatch.setattr(driver, "run", refused)
    monkeypatch.setattr(driver, "seeding_elsewhere", lambda folder: False)
    monkeypatch.setattr(driver, "prepare_work", lambda ctx, folder, log: tmp_path / "work")
    ctx = {"run": "r1", "dir": tmp_path / "batch", "work": tmp_path / "cache"}
    assert driver.position(folder, ctx, log) == ("seed", 1)
    assert driver.load(folder / "world/SEED_FAILED.json")["reason"] == reason
    # It is still a failure of this world and still counts: a budget that is spent will not be
    # unspent by retrying, so three of these are terminal the way three of anything else are.
    assert driver.strikes(folder) == 1
    assert not driver.outage(log), "a spent budget is this world's answer, not a provider outage"


def test_a_seed_marker_that_counts_no_failure_of_this_world_is_dropped(folder, tmp_path):
    """13 of the 37 markers record rc -2, a kill from outside this driver, and count 15 strikes --
    two of them hold littler-mendelson and nucor at 2 of 3 on signals alone. Nine more sit beside a
    SEED.json that a later seeding wrote: only a seed step that runs unlinks one, and a company whose
    world exists never runs one, so three of those would retire a company with a finished world."""
    log = tmp_path / "acme.log"
    put(folder / "world/SEED_FAILED.json", {"count": 3, "rc": -2, "at": "then"}, age=10)
    assert driver.strikes(folder) == 0
    assert driver.seed_not_failed(None, folder, log) == 0  # inspection reads, and writes nothing
    assert (folder / "world/SEED_FAILED.json").is_file()
    assert driver.seed_not_failed({}, folder, log) == 0
    assert not (folder / "world/SEED_FAILED.json").is_file(), "a run drops the marker it cannot count"
    put(folder / "world/SEED_FAILED.json", {"count": 3, "rc": 1, "at": "then"}, age=10)
    assert driver.strikes(folder) == 3
    assert driver.seed_not_failed({}, folder, log) == "seed_failed_repeatedly"
    put(folder / "world/SEED.json", {"status": "seeded_reviewed"}, age=20)  # seeding worked after it
    assert driver.strikes(folder) == 0 and driver.seed_not_failed(None, folder, log) == 0
    put(folder / "world/SEED.json", {"status": "seeded_reviewed"}, age=5)  # ... and before it
    assert driver.strikes(folder) == 3


def test_a_gates_own_rules_are_an_input_of_its_verdict(folder):
    """A change to what a render or a visual judge counts as a failure changes the verdict with no
    state file and no proxy file moving. The content floor landed on 2026-09-10 and reached the whole
    cohort only because placeholder_images.py happened to change in the same round."""
    from company_envs.world import render_check

    assert driver.WORLD_SRC / "render_check.py" in driver.needed(folder, "render", driver.RENDER)
    assert driver.WORLD_SRC / "visual_judge.py" in driver.needed(folder, "visual", driver.VISUAL)
    # The phrase the driver matches to tell a repairable floor miss from the app's own faults. It is
    # render_check's wording, pinned here so a rewording fails this test instead of the repair path.
    reason = render_check.content_floor(0, 9, 0, "x" * (render_check.APP_DOM + 1))
    assert reason and driver.RENDER_FLOOR in reason


def test_a_render_that_fails_only_on_the_content_floor_gets_one_world_repair(
    folder, fake_pipeline, monkeypatch
):
    """render_failed is not terminal and is retried every loop, so the two companies whose calendars
    show an empty current week would pay a build and an eight-page render for ever and never advance.
    A floor miss is a seed that put the records outside the visible window, and world-repair rewrites
    records, so it earns one attempt -- and only when the whole failure is the floor, because two
    hours of model calls cannot reach a broken image or a page with nothing to click."""
    commands, ctx, log = fake_pipeline
    base = fake_cli(folder, commands)
    floor = "the first view shows 1 of 40 of this company's own values in 120 visible characters"
    report = {"ok": False, "apps": {}}

    def run(cmd, log, timeout):
        rc = base(cmd, log, timeout)
        if cmd[3] == "render-check":  # the gate's real verdict replaces the fake's passing one
            age = (folder / driver.RENDER).stat().st_mtime - CLOCK + 0.5
            put(folder / driver.RENDER, report, age=age)
        return rc

    monkeypatch.setattr(driver, "run", run)
    report["apps"] = {"calendar_mock": {"ok": False, "error": floor}, "gmail": {"ok": True, "error": None}}
    assert driver.position(folder, ctx, log) == ("render", "render_repaired")
    assert commands[-2:] == ["render-check", "world-repair"]
    assert driver.load(folder / driver.RENDER_REPAIR)["apps"] == {"calendar_mock": floor}
    commands.clear()
    assert driver.position(folder, ctx, log) == ("render", "render_failed")
    assert "world-repair" not in commands, "the receipt keeps the repair to one attempt"
    # A failure the world cannot explain never buys a repair at all.
    (folder / driver.RENDER_REPAIR).unlink()
    report["apps"] = {"calendar_mock": {"ok": False, "error": "nothing on the page can be clicked"}}
    commands.clear()
    assert driver.position(folder, ctx, log) == ("render", "render_failed")
    assert commands == ["render-check"] and not (folder / driver.RENDER_REPAIR).exists()
    assert driver.position(folder) == ("render", "pending")  # inspection repairs nothing


# One 44-word sentence and not a code in it: `dense` on sentence length alone, the way
# childrens-aid's briefs are, so the test turns on the readability verdict and not on jargon.
DENSE_BRIEF = (
    "Ask Dana to reconcile the three supplier invoices against the approved purchase orders and the "
    "receiving notes, then write one note for the September 11, 2026 review explaining every "
    "difference she finds and what she recommends the team should do about each of them."
)


def briefed(folder, brief, *, age=4):
    """A company that has been through export, rewrite and apps, with BRIEF as its one task's text.

    Both files, because the two gates read different ones: the readability report measures the
    public assignment and the relative-time rule reads the workflow, exactly as the agreement stage
    does.
    """
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    put(folder / "tasks/t1/workflow.json", {"id": "t1", "brief": brief}, age=age)
    put(folder / "tasks/t1/assignment.json", {"workflow_id": "t1", "brief": brief}, age=age)
    return folder


def test_a_brief_the_agreement_will_refuse_stops_the_company_before_its_seed(
    folder, fake_pipeline, monkeypatch
):
    """Two of the nine agreement conditions need no world -- `readable` is REPORT-readability's brief
    category and `evergreen` is relative time in a brief -- and both were computed in the last stage
    of the pipeline, after the ~33-hour seed, the check, the render, the visual judge, the
    calibrations and the VM fleet. Measured over the 99 companies on 2026-09-10, two were already
    refused by their own briefs and had spent 92 recorded core-hours of seeding calls between them:
    clearscale's says "describe today" (601 calls, 53.7 hours, still no world) and childrens-aid's
    read `dense` (604 calls, 38.2 hours, seeded).
    """
    commands, ctx, log = fake_pipeline
    briefed(folder, "Tell Dana what we can describe today and send it on September 11, 2026.")
    assert driver.position(folder, ctx, log) == ("seed", "briefs_not_plain")
    assert "seed-world" not in commands, "the gate runs before the expensive stage, not after it"
    assert "briefs_not_plain" in driver.TERMINAL  # the agreement would refuse it; nothing repairs it
    assert "brief uses relative time (today)" in log.read_text()
    # The readability half: a brief no relative-time rule touches, refused for how it reads.
    commands.clear()
    briefed(folder, DENSE_BRIEF)
    assert driver.position(folder, ctx, log) == ("seed", "briefs_not_plain")
    assert "seed-world" not in commands
    assert "briefs read dense" in log.read_text()
    assert driver.position(folder) == ("seed", "briefs_not_plain")  # visible with no log to write to
    # The same verdict the late gate reaches, from the same code rather than a restatement of it.
    from company_envs.world.readability import readability_report

    assert readability_report(folder)["blocking"] == ["brief"]
    # A reworded brief un-blocks the company with no marker to delete: the gate keeps none.
    commands.clear()
    briefed(folder, "Tell Dana what we can promise, and send it on September 11, 2026.", age=5)
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert commands[0] == "seed-world"


def test_a_seeded_world_is_never_held_back_by_its_briefs(folder, fake_pipeline):
    """Before the seed the repair is `rewrite-briefs --again`, two calls on an 8 KB payload; after it
    the same repair re-dates REWRITE.json, which re-dates ASSIGN.json, which re-seeds the world. So a
    gate that fired late would demand 33 hours to fix a sentence, which is the standing rule about
    never blocking on a defect the pipeline cannot repair. childrens-aid, whose briefs read dense, had
    already paid for its seed on 2026-09-10: it goes on to the gates that do decide it."""
    _, ctx, log = fake_pipeline
    briefed(folder, DENSE_BRIEF)
    assert driver.briefs_ready(ctx, folder, log) == "briefs_not_plain"
    put(folder / "world/SEED.json", {"status": "seeded_reviewed"}, age=5)
    assert driver.briefs_ready(ctx, folder, log) == 0
    assert driver.brief_problems(folder)[0], "the finding stands; what changes is what it may cost"


def test_briefs_with_nothing_to_measure_are_not_a_verdict(folder, fake_pipeline):
    """An empty run must not write the receipt a real failure writes. A company with no published
    brief measures as `unmeasured`, which says there was nothing to read, and blocking on it would
    make every folder without a task terminal; an assignment that is not readable as a brief is
    reported and not counted either."""
    _, ctx, log = fake_pipeline
    put(folder / "company.json", {}, age=1)
    put(folder / "tasks/_plain_brief/REWRITE.json", {}, age=2)
    put(folder / "tasks/_worker_apps/ASSIGN.json", {}, age=3)
    assert driver.brief_problems(folder) == ([], None)
    assert driver.briefs_ready(ctx, folder, log) == 0
    put(folder / "tasks/t1/assignment.json", {"workflow_id": "t1", "brief": ["not", "text"]}, age=4)
    problems, unmeasured = driver.brief_problems(folder)
    assert problems == [] and "brief must be a string" in unmeasured
    assert driver.briefs_ready(ctx, folder, log) == 0
    assert "briefs could not be measured" in log.read_text()


def test_no_rule_of_the_mechanical_check_can_move_before_the_seed(folder):
    """The other half of the ordering answer. world-check judges authored records, so none of it is
    computable before the authoring -- and it already runs inside the seed, over the world as it is
    about to be written, where `authoring=True` makes its structural rules errors the author can
    still act on. The driver's check stage is the second reading, of the world on disk by the current
    checker, and that one cannot move earlier than the records it reads."""
    import inspect

    from company_envs.world import state_seed, world_check

    signature = inspect.signature(world_check.check_world).parameters
    assert {"states", "identities", "world", "bulk_ids"} <= set(signature), "every rule reads records"
    assert "check_folder" in inspect.getsource(state_seed), "the seed runs the same check as it writes"
    assert driver.INPUTS["check"](folder)[0] == folder / "world/BULK.json"  # the finished world


def test_the_three_ways_to_mine_nothing_are_three_different_receipts():
    """`MINE.json` recorded `requested: 2, accepted: []` for a designer that looked and refused, for a
    payload over the provider's ceiling and for a world already at target with nothing asked for. The
    first two are verdicts and the third is a pass, and two of the three wrote no note at all.

    `mine_outcome` is the one place that decides, so a fourth way to mine nothing has somewhere to go
    instead of inheriting whichever reading the last author happened to leave.
    """
    at_target = {"accepted": [], "rejected": []}
    assert driver.mine_outcome(at_target, 0).outcome == "passed", "nothing was asked for"
    assert driver.mine_outcome({"accepted": ["t1"], "rejected": []}, 2).outcome == "passed"
    refused = driver.mine_outcome({"accepted": [], "rejected": [], "note": "over the ceiling"}, 2)
    assert refused.outcome == "refused" and refused.reason == "over the ceiling"
    reviewed = driver.mine_outcome({"accepted": [], "rejected": ["t2", "t3"]}, 2)
    assert reviewed.outcome == "refused" and "rejected 2" in reviewed.reason
    # The case that had no words at all: it still cannot read as a finished mining pass.
    silent = driver.mine_outcome({"accepted": [], "rejected": []}, 2)
    assert silent.outcome == "refused" and "recorded no reason" in silent.reason


def deciders_of(marker):
    """The modules that decide MARKER's verdict, derived from the source rather than from the list.

    Two derivations, intersected. The first reads ``__main__``'s dispatch: a command's branch names
    the module it runs, so ``world-check`` points at world_check.py without anyone saying so. The
    second reads every module for a write call whose path is built from the marker's own name, which
    is what makes the first precise -- the one step behind RENDER.json, VISUAL.json and the
    calibrations runs five commands, and only render_check.py writes a RENDER.json.

    It is an audit and not the table, and it finds the module that writes the verdict rather than
    every module that changes what the verdict is taken of: it names render_check.py for a render
    and not the three serving files whose pages the render reads, which stay a judgement. The
    intersection is empty for REVIEW-SHEET.md, whose path review_sheet.py assembles from a variable,
    and for the markers the driver writes itself, which run no command. What it does catch is a
    module that starts writing a marker later, and a step added with no decision recorded about it.
    """
    import ast
    import inspect

    src = Path(driver.ROOT) / "src" / "company_envs"
    name = str(marker).rsplit("/", 1)[-1] if isinstance(marker, str) else ""

    def literals(node):
        return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    def writes(tree, known):
        """Whether this tree writes the marker: its basename in the path argument of a write call."""
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            function = (
                call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
            )
            if function not in ("write", "write_json", "write_text", "write_bytes", "replace"):
                continue
            target = call.func.value if isinstance(call.func, ast.Attribute) else (call.args or [None])[0]
            if target is None:
                continue
            found = literals(target) | {
                known[n.id] for n in ast.walk(target) if isinstance(n, ast.Name) and n.id in known
            }
            if any(text == name or text.endswith("/" + name) for text in found):
                return True
        return False

    def constants(tree):
        return {
            t.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for t in node.targets
            if isinstance(t, ast.Name) and isinstance(node.value.value, str)
        }

    # Every module that writes the marker, and every module a dispatch branch writing it imports.
    writers, by_command = set(), {}
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        if path.name == "__main__.py":
            for node in ast.walk(tree):
                command = (
                    node.test.comparators[0].value
                    if isinstance(node, ast.If)
                    and isinstance(node.test, ast.Compare)
                    and isinstance(node.test.left, ast.Attribute)
                    and node.test.left.attr == "command"
                    and isinstance(node.test.comparators[0], ast.Constant)
                    else None
                )
                if command is None:
                    continue
                branch = ast.Module(body=node.body, type_ignores=[])
                modules = {
                    src.joinpath(*(i.module or "").split(".")[-2:]).with_suffix(".py")
                    for i in ast.walk(branch)
                    if isinstance(i, ast.ImportFrom) and i.level
                }
                by_command[command] = {p for p in modules if p.is_file()}
                if writes(branch, constants(tree)):
                    writers |= by_command[command]
        elif writes(tree, constants(tree)):
            writers.add(path)

    # The commands the steps behind this marker run, read from the driver's own source. A step that
    # delegates to another (render_step calls calibrate, which is where render-check runs) is followed.
    def commands_run(step, seen=()):
        if step.__name__ in seen:
            return set()
        source = inspect.getsource(step)
        found = set(re.findall(r'CLI \+ \["([a-z-]+)"', source))
        if getattr(step, "command", None):
            found.add(step.command)
        for call in ast.walk(ast.parse(source.lstrip())):
            inner = getattr(call.func, "id", None) if isinstance(call, ast.Call) else None
            if callable(function := getattr(driver, inner or "", None)) and hasattr(function, "__name__"):
                found |= commands_run(function, (*seen, step.__name__))
        return found

    commands = {c for _, spec, _, step in driver.STEPS if spec == marker for c in commands_run(step)}
    return writers & {module for command in commands for module in by_command.get(command, ())}


def test_every_marker_names_the_code_that_decides_it_or_records_why_it_must_not(folder):
    """Five of the sixteen markers had no code input of any kind, so no fix could reach them.

    Measured on 2026-09-10, before this test: world/NAMES.json, world/worker_apps.json,
    REVIEW-SHEET.md and AGREEMENT.json were decided by names.py, state_seed.py, review_sheet.py and
    agreement.py, which took 4, 42, 5 and 13 of that week's 295 commits -- 56 between them -- and
    could not re-date a marker; company.json was the fifth and must stay that way. Listing the code per stage
    was why: a stage's list has to suit its cheapest step and its most expensive one at once.

    The table is hand-written on purpose -- a digest of the transitive imports would restale
    CHECKS.json on 111 of those 295 commits against 28 for the list, because ten modules of the
    world core are one import cycle. What is derived is this audit: every module the source says decides a marker has to
    be an input of it, written down as not deciding it, or the marker has to say why it takes no code
    input at all. A step added later with none of the three fails here instead of silently keeping an
    old verdict for ever.
    """
    markers = [m for _, m, _, _ in driver.STEPS if m is not None]
    code = driver.deciding_code()
    for marker in markers:
        named = marker if isinstance(marker, str) else marker.__name__
        assert (marker in code) != (marker in driver.NO_CODE_INPUT), f"{named}: one table or the other"
        assert len(driver.NO_CODE_INPUT.get(marker, "x" * 40)) > 30, f"{named}: say why, with the cost"
        listed = set(code.get(marker, ())) | set(driver.WRITES_NOT_DECIDES.get(marker, ()))
        if marker in code:
            missed = deciders_of(marker) - listed
            assert not missed, f"{named} is decided by {sorted(p.name for p in missed)}"
    assert len(markers) == len(set(markers)) == 16  # one decision per marker, and sixteen of them
    # The audit is real: it finds the module behind each of these, and would fail if one were dropped.
    for marker, module in (
        ("world/CHECKS.json", "world_check.py"),
        ("world/worker_apps.json", "state_seed.py"),
        ("world/NAMES.json", "names.py"),
        ("REPORT-readability.json", "readability.py"),
        (driver.RENDER, "render_check.py"),
        (driver.VISUAL, "visual_judge.py"),
        ("AGREEMENT.json", "agreement.py"),
    ):
        assert driver.WORLD_SRC / module in deciders_of(marker), marker


def test_the_four_markers_no_fix_could_reach_now_go_stale_with_their_own_code(folder, monkeypatch, tmp_path):
    """A fix to names, the worker-app sync, the review sheet or the agreement reached no company.

    Each is seconds of CPU to retake -- dedupe-names 1 s (max 9), sync-worker-apps under 1 s,
    review-sheet 1 s, agreement 64 s, from the per-company logs -- so there was never a cost reason
    for the omission; the reason was that the code was listed per stage. Each one's marker now goes
    stale for its own module and for nobody else's.
    """
    seeded(folder)
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=50)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=51)
    put(folder / "runtime/grades/t1/calibration.json", {"accepted": True, "scope": "all_checks"}, age=52)
    put(folder / "runtime/tasks/t1/CONTROLLER.json", {"status": "done"}, age=53)
    put(folder / "REVIEW-SHEET.md", sheet_text(), age=54)
    put(folder / "AGREEMENT.json", {"done": True, **stamped("agreement")}, age=55)
    for name in CODE_TUPLES:
        monkeypatch.setattr(driver, name, (put(tmp_path / f"{name.lower()}.py", None, age=0, base=0),))
    assert driver.position(folder) == ("done", 0)
    for tuple_name, stage in (
        ("NAMES_CODE", "seed"),
        ("SYNC_CODE", "check"),
        ("SHEET_CODE", "sheet"),
        ("AGREEMENT_CODE", "agreement"),
    ):
        fix = put(tmp_path / f"fix_{tuple_name}.py", None, age=100)  # lands after every marker
        monkeypatch.setattr(driver, tuple_name, (fix,))
        assert driver.position(folder) == (stage, "pending"), tuple_name
        monkeypatch.setattr(
            driver, tuple_name, (put(tmp_path / f"{tuple_name.lower()}.py", None, age=0, base=0),)
        )
    assert driver.position(folder) == ("done", 0)  # and with every fix adopted, nothing is pending


def test_a_re_render_does_not_re_run_the_episodes_under_it(folder, monkeypatch, tmp_path):
    """vm_inputs held RENDER.json and VISUAL.json, which gave the one stage that must list no code
    six source files through render_inputs: the five files behind a render or a visual verdict took
    39 of the 295 commits in the week to 2026-09-10, and each of them re-ran every episode on disk --
    120 episodes that week at three checkpoints, over 2,000 at the 50-company target against a fleet
    of eight slots. A recomputed opinion moves no record; the calibrations speak for the records."""
    seeded(folder)
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=50)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=51)
    put(folder / "runtime/grades/t1/calibration.json", {"accepted": True, "scope": "all_checks"}, age=52)
    put(folder / "runtime/tasks/t1/CONTROLLER.json", {"status": "done"}, age=53)
    put(folder / "REVIEW-SHEET.md", sheet_text(), age=54)
    put(folder / "AGREEMENT.json", {"done": True, **stamped("agreement")}, age=55)
    for name in CODE_TUPLES:
        monkeypatch.setattr(driver, name, (put(tmp_path / f"{name.lower()}.py", None, age=0, base=0),))
    assert driver.position(folder) == ("done", 0)
    # A proxy fix re-renders and re-judges; the episode under them stands, and so does the agreement.
    monkeypatch.setattr(driver, "PROXY_CODE", (put(tmp_path / "hub_app.py", None, age=100),))
    assert driver.position(folder) == ("render", "pending")
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=101)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=102)
    assert driver.position(folder) == ("done", 0)
    # A record that really moved is a different thing: it re-dates the calibration, and the episode
    # under that calibration is redone like any other stale marker.
    put(folder / "world/drive_mock.state.json", {"files": []}, age=103)
    assert driver.position(folder) == ("render", "pending")
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, **stamped("render_check")}, age=104)
    put(folder / "runtime/VISUAL.json", {"ok": True, "apps": {}, **stamped("visual_judge")}, age=105)
    assert driver.position(folder) == ("calibrate", "pending")
    put(folder / "runtime/grades/t1/calibration.json", {"accepted": True, "scope": "all_checks"}, age=106)
    assert driver.position(folder) == ("vm", "pending")


def test_nothing_above_the_seed_may_take_a_code_input(folder):
    """The guard on the most expensive mistake this table can make.

    company.json re-dates REWRITE.json, REWRITE.json re-dates ASSIGN.json and ASSIGN.json re-dates
    world/SEED.json, so a code input anywhere above the seed would re-seed the cohort: 60 worlds at
    ~33 hours of model calls each, about 2,000 core-hours, for a 2 s re-export. Checked on disk
    2026-09-10: 0 of the 60 seeded worlds held an upstream marker newer than their SEED.json.
    """
    chain = ("company.json", driver.REWRITE, driver.ASSIGN, "world/SEED.json", "world/BULK.json")
    code = driver.deciding_code()
    for marker in chain:
        assert marker not in code and marker in driver.NO_CODE_INPUT, marker
        assert not [p for p in driver.needed(folder, "seed", marker) if p.suffix == ".py"], marker
    # BULK.json is the exception that proves the rule: its freshness is a version recorded in the
    # marker, bumped by hand when records land differently, not the mtime of bulk_layer.py -- which
    # took 12 of that week's commits against the one bump, each of them 243 s a world of model calls
    # and a rewritten world/*.state.json that re-dates every calibration under it.
    assert driver.bulk_ok(LAID) and not driver.bulk_ok({"bulk_version": BULK_VERSION - 1})


def test_a_tie_reads_fresh_because_reading_it_as_stale_re_seeds_the_cohort(folder, tmp_path):
    """fresh() compares with a strict `>`, and the one-character hardening is unaffordable.

    The dates this rule compares are not a record of when a verdict was reached: 6,207 of the 6,912
    files under companies/ were created inside one three-minute window on 2026-09-10 by an operation
    outside the pipeline, and files from different stages share an mtime to the nanosecond. 105 of the
    836 artifact comparisons on disk are such ties -- 72 ASSIGN.json against their own REWRITE.json,
    13 CHECKS.json against BULK.json, 11 VISUAL.json against RENDER.json.

    A tie carries no ordering, so either reading is defensible in the abstract and the cost decides.
    Reading a tie as stale rewrites those 72 ASSIGN.json; a rewritten ASSIGN.json postdates
    world/SEED.json, and 36 of the 72 are seeded: ~1,188 core-hours of re-seeding for a comparison
    that was never evidence of anything. The marker stands, and what closes the hole properly is a
    freshness value recorded in the marker, which is what bulk_ok already reads.
    """
    marker = put(tmp_path / "CHECKS.json", {"ok": True}, age=10)
    tied = put(tmp_path / "BULK.json", LAID, age=10)
    newer = put(tmp_path / "STATE.json", {}, age=11)
    assert marker.stat().st_mtime == tied.stat().st_mtime
    assert driver.fresh(marker, [tied]), "a tie is not evidence the input is newer"
    assert not driver.fresh(marker, [newer]), "a real ordering still restales"
    # And the mechanism that does survive a re-dating: a value the marker carries itself.
    assert driver.fresh(tied, [], driver.bulk_ok) and not driver.fresh(tied, [], lambda m: False)
    assert not driver.bulk_ok({}), "a marker with no recorded version is stale whatever its date"


@pytest.mark.parametrize(
    ("report", "admitted", "why"),
    [
        ({"ok": True, "outcome": "passed", "apps": {"drive_mock": {"ok": True}}}, True, "every app rendered"),
        ({"ok": False, "outcome": "refused", "apps": {"drive_mock": {"ok": False}}}, False, "an app failed"),
        # The host failed. It says nothing about the world, and no stage can install a browser, so
        # blocking would be a permanent stall on a defect the pipeline cannot repair.
        (
            {"ok": False, "outcome": "faulted", "skipped": "no headless browser found", "apps": {}},
            True,
            "the host cannot render at all",
        ),
        # Nothing was served, so nothing was looked at. This used to be `ok: True` -- the same receipt
        # a render of every app passing writes -- and it is what let a company reach the VM stage
        # having never been rendered.
        ({"ok": None, "outcome": "unmeasured", "apps": {}}, False, "a company nobody looked at"),
        ({}, False, "no report is no evidence"),
    ],
)
def test_the_render_and_visual_gates_ask_the_module_that_knows_what_a_render_is(report, admitted, why):
    """The gate and the CLI's exit code are the same predicate, so they cannot drift apart.

    `render_ok` was `ok is True` and the skip wrote `ok: True`, which is how "this host has no Chrome"
    and "every app rendered" became one answer -- and "nothing was served" with them. Separating the
    fault from the empty run at the source means neither gate needs a judgement about the fleet's
    hosts: the receipt says which it is, and the rule is stated once in render_check.admissible.
    """
    from company_envs.world.render_check import admissible

    assert driver.render_ok(report) is admitted, why
    assert admissible(report) is admitted, "one predicate, two callers"
    if report:
        assert driver.visual_ok(report) is admitted, why


def test_a_markers_recorded_value_names_the_same_files_the_driver_lists(folder):
    """The two halves of deciding_code() have to name the same files or one of them is a lie.

    The driver compares a marker's mtime against the files this table names; the module that writes
    the marker records a digest of the files *its* DECIDES_THIS names, and the driver's accept slot
    asks that module whether the value is current. If the two tuples drift, the list restales markers
    the digest calls current, or the digest blesses a verdict the list would have retaken -- and
    neither shows up as a failure anywhere. Pinning them equal is what makes two mechanisms one.

    Three markers are not migrated yet: RENDER.json and VISUAL.json wait on render_check.py and
    visual_judge.py, and worker_apps.json on state_seed.py. Each of those is two lines in a file that
    belonged to another agent on 2026-09-10, and the exact text is in the report.
    """
    from importlib import import_module

    code = driver.deciding_code()
    migrated = {}
    for _, marker, accept, _ in driver.STEPS:
        if marker not in code:
            continue
        if not (named := getattr(accept, "decides", None)):
            continue
        module = import_module(f"company_envs.world.{named}")
        migrated[str(marker)] = module
        assert set(map(str, module.DECIDES_THIS)) == set(map(str, code[marker])), marker
    assert sorted(migrated) == [
        "AGREEMENT.json",
        "REPORT-readability.json",
        "REVIEW-SHEET.md",
        "runtime/RENDER.json",
        "runtime/VISUAL.json",
        "world/CHECKS.json",
        "world/NAMES.json",
    ]
    # Seven of the eight. world/worker_apps.json is the one that declines a recorded value: it is a
    # bare map whose top-level keys *are* the worker ids, read in 27 places across 14 modules, so a
    # decided_by key would be enumerated as a worker named decided_by by every one of them. It is also
    # the cheapest marker here to retake, under 1 s with no cascade, and state_seed.py's 42 commits in
    # the week restale it on nearly every loop anyway, so its exposure to a re-dating is the shortest.
    assert "world/worker_apps.json" in driver.deciding_code()
    assert next(a for _, m, a, _ in driver.STEPS if m == "world/worker_apps.json") is None
    # And every one of them refuses a marker that records nothing, which is the migration: 199 of the
    # 247 markers on disk were already stale on their code, so adopting this during the freeze retook
    # 48 -- 42 dedupe-names at 1-9 s and 6 readability reports at 1-8 s.
    for marker, module in migrated.items():
        assert not module.current({}), marker
        assert not module.current(None), marker
        # decided_by digests a path that does not exist as its absence rather than raising, so a typo
        # in one of these tuples would be a permanent freshness wearing the fix's clothes.
        assert all(Path(p).is_file() for p in module.DECIDES_THIS), marker


def test_a_recorded_value_survives_a_re_dating_and_an_mtime_does_not(folder, tmp_path, monkeypatch):
    """The defect the recorded value exists for, in one pass.

    6,207 of the 6,912 files under companies/ were created inside one three-minute window on
    2026-09-10 by an operation outside the pipeline, so 93 markers read as newer than the last change
    to the module that decides them with nothing able to say whether the verdict came from it. Here
    the marker is re-dated to *after* a changed checker, exactly as that operation did it: the mtime
    half is satisfied and says the verdict is current, and the recorded half still says it is not.
    """
    from company_envs.world import world_check

    seeded(folder)
    checks = folder / "world/CHECKS.json"
    monkeypatch.setattr(driver, "CHECK_CODE", (put(tmp_path / "checker.py", None, age=0, base=0),))
    assert driver.fresh(
        checks, driver.needed(folder, "check", "world/CHECKS.json"), driver.recorded("world_check")
    )
    # A checker that changed after the verdict: both halves agree it is stale.
    later = put(tmp_path / "checker.py", None, age=10_000)
    monkeypatch.setattr(driver, "CHECK_CODE", (later,))
    stale = driver.needed(folder, "check", "world/CHECKS.json")
    assert not driver.fresh(checks, stale, driver.recorded("world_check"))
    # Now re-date the marker over it, which is all that operation did. The mtime half is fooled.
    put(checks, {"ok": True}, age=20_000)
    assert driver.fresh(checks, driver.needed(folder, "check", "world/CHECKS.json")), "the mtime says current"
    assert not world_check.current(driver.load(checks)), "the content says it was not this checker"
    assert not driver.fresh(
        checks, driver.needed(folder, "check", "world/CHECKS.json"), driver.recorded("world_check")
    )


def test_the_accept_slot_is_a_re_run_trigger_and_not_a_gate(folder, fake_pipeline, monkeypatch):
    """Three verdicts that look like obvious content checks to add, and cost what they cost.

    A marker the accept slot refuses makes its step run again; it does not stop the company. So a
    verdict belongs here only when re-running is a plausible remedy for it, which is true of all five
    present -- a layer laid by old code is re-laid, a failed render re-rendered, a state-only proof
    re-calibrated, an unfinished checkpoint resumed, a verdict from a changed module retaken.

    Measured on the cohort 2026-09-10, the three proposed additions:
      receipt.passed on world/SEED.json refuses the 50 of 60 worlds whose status is
        seeded_review_failed, which re-seeds them at ~33 hours each, ~1,650 core-hours -- and
        review_accepted already stops each of them for nothing.
      `done is True` on AGREEMENT.json re-runs a 64 s agreement every loop for a company whose
        conditions have not changed, without end; trex-company fails vm_verified and no number of
        re-runs changes that. A refused company is a verdict, and cohort_report counts it as one.
      `not blocking` on REPORT-readability.json re-reads prose that no stage rewrites.
    Each of the three has a gate row already, which stops the company and costs nothing.
    """
    from company_envs import receipt

    commands, ctx, log = fake_pipeline
    assert driver.position(folder, ctx, log) == ("done", 0)
    rows = {str(m): a for _, m, a, _ in driver.STEPS if m is not None}
    for marker in ("world/SEED.json", driver.REWRITE, driver.ASSIGN):
        assert rows[marker] is None, f"{marker}: a verdict here re-runs the step above the seed"
    # A rejected world is what the refused check would have re-seeded, and the gate holds it instead.
    put(folder / "world/SEED.json", {"status": "seeded_review_failed"}, age=300)
    assert not receipt.passed(driver.load(folder / "world/SEED.json")), "the field says refused"
    commands.clear()
    assert driver.position(folder, ctx, log) == ("seed", "review_not_accepted")
    assert "seed-world" not in commands, "the gate stopped it; the accept slot would have re-seeded it"
    # The agreement's own verdict is read where a reader needs it, not where it would spin.
    assert rows["AGREEMENT.json"].__name__ == "agreement_decided_it"
    assert driver.status_view([("r1", "acme")], folder.parent, {})["companies"]["acme"].get("completed_at")


def test_a_render_stale_only_on_its_digest_is_actually_re_rendered(folder, fake_pipeline, monkeypatch):
    """The stall the recorded value nearly introduced, and the reason accept_for exists.

    calibrate() decides for itself whether each of its three parts has work to do. It named the
    predicates directly while STEPS named them too, so when RENDER.json gained a recorded freshness
    value the two disagreed immediately: position() saw the marker stale on its digest and ran the
    step, the step asked only render_ok, found `ok: true`, served a hub and rendered nothing, and the
    next loop did the same. Twenty companies on disk, every loop, with no marker ever rewritten --
    the defect this table exists to stop, reintroduced by the fix for it. accept_for reads the
    predicate out of the table so there is only one statement of the rule.
    """
    commands, ctx, log = fake_pipeline
    assert driver.position(folder, ctx, log) == ("done", 0)
    # A RENDER.json whose verdict is fine and whose digest is an older serving stack's.
    commands.clear()
    put(folder / "runtime/RENDER.json", {"ok": True, "apps": {}, "decided_by": "older stack"}, age=300)
    assert driver.render_ok(driver.load(folder / "runtime/RENDER.json")), "the verdict alone says fine"
    assert driver.position(folder) == ("render", "pending")
    assert driver.position(folder, ctx, log) == ("done", 0)
    assert "render-check" in commands, "the step ran the render, rather than serving a hub for nothing"
    assert driver.position(folder) == ("done", 0)  # and the rewritten marker records today's stack
    # Both halves are asked of both markers: a verdict, and the stack that reached it.
    for marker, fault in ((driver.RENDER, {"ok": False}), (driver.VISUAL, {"ok": False})):
        accept = driver.accept_for(marker)
        assert not accept({**driver.load(folder / marker), **fault}), f"{marker}: verdict unread"
        assert not accept({**driver.load(folder / marker), "decided_by": "older stack"}), marker
        assert accept(driver.load(folder / marker)), marker
