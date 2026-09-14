"""Resumable company episodes; all generated files live below the source runtime/.

Every task of a company gets its own runtime: ``runtime/tasks/<task_id>/CONTROLLER.json`` is the
checkpoint and ``runtime/tasks/<task_id>/controller/`` holds the working copy of the company
(``company/``), step artifacts and the history of set-aside episodes. Company-level files that are
not per task (``runtime/grades/<task>/calibration.json`` from the driver's calibrate stage,
``runtime/golden``, ``runtime/logs``, hub-serve's endpoints and sessions) stay where they were. A
checkpoint written before this layout, ``runtime/CONTROLLER.json`` with ``runtime/controller/``,
is still read in place for the task it names (see ``task_runtime``).

``budgets = {"seconds": {step: seconds, ...}, "model_calls": count}`` is the
only budget input. Seconds bound each attempt; model reservations survive retries
and forced restarts (including reset). A reservation counts a Models.call,
including cache hits; the real adapter disables provider fallback.

Steps are trusted synchronous implementations, with cooperative deadline checks.
A Linux main-thread alarm interrupts blocking Python calls, but cannot promise
cancellation of arbitrary remote effects. Failed steps never admit later steps.
A step the teacher's verdict makes pointless is recorded ``status: "done"`` with ``skipped: true``
and a reason (see ``_skip``): done, because the resume loop reads that field to decide what to
re-run and a skip must not be retried on every re-entry; ``skipped``, because a row marked done
without running is not a prerequisite a restart may begin after (see ``_completed``).
Dry runs stage payloads and run scripted policies, with no model, npm, or VM calls;
their grade artifact explicitly says that no business outcome was graded.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from company_envs.config import load_config
from company_envs.receipt import PASSED, UNMEASURED, Receipt, outcome_of
from company_envs.storage import now, read, run_lock, write

# The insider-access teacher runs before the graded worker episodes: it is the feasibility
# proof, and a task it cannot complete does not get a worker episode.
# load: world state into the hub apps (the pipeline's "seed" generates the world; this only loads
# it). The insider teacher runs before the worker VMs launch: it boots its own VMs, and a task it
# cannot complete never launches the workers' machines.
# After launch, every configured no-hint worker policy gets its own "episode:<policy>" and
# "grade:<policy>" pair; policies after the first start from a fresh session (the reset step).
BASE_STEPS = ("load", "check", "serve", "calibrate", "teacher", "launch")
POLICY_STEPS = ("episode", "grade")
FINAL_STEPS = ("teardown",)
DEFAULT_POLICIES = ("own",)
DEFAULT_SECONDS = 1500
_POLICY_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _policies(value):
    policies = list(value) if isinstance(value, (list, tuple)) else None
    if (
        not policies
        or len(set(policies)) != len(policies)
        or any(not isinstance(p, str) or not _POLICY_NAME.fullmatch(p) for p in policies)
    ):
        raise ValueError("worker_policy.policies must be a non-empty list of distinct policy names")
    return policies


def step_names(policies=DEFAULT_POLICIES):
    """The controller's step list for these worker policies, in execution order."""
    pairs = (f"{kind}:{policy}" for policy in _policies(policies) for kind in POLICY_STEPS)
    return (*BASE_STEPS, *pairs, *FINAL_STEPS)


def configured_policies(root):
    """``[worker_policy] policies`` from config.toml; our own policy when unset."""
    config = load_config(root, missing_ok=True)
    return _policies(config.get("worker_policy", {}).get("policies", DEFAULT_POLICIES))


def _kind(step):
    return step.partition(":")[0]


def _policy_of(step, default=DEFAULT_POLICIES[0]):
    return step.partition(":")[2] or default


def _artifact_name(step):
    return step.replace(":", "-") + ".json"


STEP_NAMES = step_names()  # the default (single own policy) list
DEFAULT_BUDGETS = {"seconds": dict.fromkeys((*STEP_NAMES, "reset"), DEFAULT_SECONDS), "model_calls": 0}


class ControllerError(RuntimeError):
    def __init__(self, step: str, error: str):
        self.step = step
        super().__init__(f"{step}: {error}")


def _pending():
    # ``outcome`` is the shared word from company_envs.receipt and is deliberately *beside*
    # ``status`` rather than instead of it. ``status`` drives the resume loop -- every re-entry
    # re-runs what is not "done" -- and a skip has to read as done there or it would be re-skipped
    # for ever. ``outcome`` answers the other question, the one a reader of a dozen stages' receipts
    # asks: did this row measure anything, and if not, was that a verdict, a fault or no evidence.
    return {
        "status": "pending",
        "outcome": None,
        "started": None,
        "finished": None,
        "error": None,
        "artifacts": {},
    }


def _budgets(value, steps=STEP_NAMES):
    result = copy.deepcopy(value)
    if set(result) != {"seconds", "model_calls"}:
        raise ValueError("budgets requires seconds and model_calls")
    seconds = result["seconds"]
    if isinstance(seconds, dict):
        seconds.setdefault("teacher", DEFAULT_SECONDS)
        # Per-policy steps take a plain "episode"/"grade" entry (checkpoints written before
        # per-policy steps, CLI shorthand) or the default when the entry is missing.
        for step in steps:
            if _kind(step) in POLICY_STEPS:
                seconds.setdefault(step, seconds.get(_kind(step), DEFAULT_SECONDS))
        for kind in POLICY_STEPS:
            seconds.pop(kind, None)
    if not isinstance(seconds, dict) or set(seconds) - {*steps, "reset"} or set(steps) - set(seconds):
        raise ValueError("seconds must map every controller step to its time limit")
    seconds.setdefault("reset", seconds["teardown"])
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in seconds.values()):
        raise ValueError("step seconds must be positive and finite")
    if type(result["model_calls"]) is not int or result["model_calls"] < 0:
        raise ValueError("model_calls must be a nonnegative integer")
    return result


LEGACY_CHECKPOINT = "runtime/CONTROLLER.json"  # the one-checkpoint-per-company layout


def task_runtime(folder, task_id):
    """The directory holding this task's checkpoint and working copy.

    ``runtime/tasks/<task_id>/``, unless the company still has a checkpoint at the old location,
    ``runtime/CONTROLLER.json``, naming this task: that runtime is then read (and resumed) where
    it is. Nothing is moved automatically; an operator may
    ``mv runtime/CONTROLLER.json runtime/controller runtime/tasks/<task_id>/`` to adopt the layout.
    """
    folder = Path(folder)
    legacy = folder / LEGACY_CHECKPOINT
    if legacy.is_file():
        try:
            if read(legacy).get("task_id") == task_id:
                return legacy.parent
        except (OSError, ValueError):
            pass
    return folder / "runtime" / "tasks" / task_id


def task_runtimes(folder):
    """Every task runtime of a company as ``{task_id: directory}``: the per-task directories that
    hold a checkpoint, and the old location for the task its checkpoint names (which, as in
    ``task_runtime``, is that task's runtime until the operator moves it)."""
    folder = Path(folder)
    found = {
        p.name: p
        for p in sorted((folder / "runtime" / "tasks").glob("*"))
        if (p / "CONTROLLER.json").is_file()
    }
    legacy = folder / LEGACY_CHECKPOINT
    if legacy.is_file():
        try:
            task = read(legacy).get("task_id")
        except (OSError, ValueError):
            task = None
        if isinstance(task, str):
            found[task] = legacy.parent
    return found


@dataclass
class Context:
    root: Path
    folder: Path
    state: dict[str, Any]
    step: str
    deadline: float
    _mutex: threading.Lock = field(default_factory=threading.Lock)
    _runtime: Path | None = None

    def __post_init__(self):
        if self._runtime is None:
            self._runtime = task_runtime(self.folder, self.state["task_id"])

    @property
    def runtime(self):
        """This task's runtime: the checkpoint, the working copy and the artifacts live here."""
        return self._runtime

    @property
    def work(self):
        return self.runtime / "controller" / "company"

    @property
    def sid(self):
        return self.state["sid"]

    @property
    def dry_run(self):
        return self.state["options"]["dry_run"]

    @property
    def policy(self):
        """The worker policy of an episode/grade step; the first configured one for plain names."""
        policies = (self.state.get("options") or {}).get("policies") or DEFAULT_POLICIES
        return _policy_of(self.step, policies[0])

    def save(self):
        write(self.runtime / "CONTROLLER.json", self.state)

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f"{self.step} seconds budget exhausted")
        return seconds

    def model_call(self, call, *args, **kwargs):
        """Reserve before dispatch, including calls that fail or are interrupted."""
        with self._mutex:
            self.remaining()
            if self.state["model_calls_used"] >= self.state["budgets"]["model_calls"]:
                from .harness import BudgetExhausted

                raise BudgetExhausted("model call budget exhausted")
            self.state["model_calls_used"] += 1
            self.save()
        return call(*args, **kwargs)

    def artifact(self, name, value):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name):
            raise ValueError("artifact name must be a filename")
        path = self.runtime / "controller" / "artifacts" / self.state["steps"][self.step]["attempt"] / name
        write(path, value)
        self.state["steps"][self.step]["artifacts"][name] = str(path.relative_to(self.runtime))
        self.save()
        return value


class Steps(Protocol):
    def run(self, step: str, context: Context) -> dict[str, Any]:
        """Return a JSON receipt; raise on failure. Check deadlines before effects.

        Implementations must recover their own interrupted effects from receipts,
        write only under context.runtime, and route inference via model_call().
        Worker steps are named ``episode:<policy>`` and ``grade:<policy>``.
        ``reset`` stops workers, restores initial apps with context.sid, and launches.
        """
        ...


class FakeSteps:
    """Offline stateful double. Fresh instances recover sessions from disk."""

    def __init__(self, *, fail=None, crash_before=None):
        self.calls = []
        self.fail = fail
        self.crash_before = crash_before

    def run(self, step, context):
        self.calls.append(step)
        if step == self.crash_before:
            raise KeyboardInterrupt(f"crash before {step}")
        if step == self.fail:
            raise RuntimeError(f"injected {step} failure")
        context.remaining()
        path = context.runtime / "controller" / "fake-apps.json"
        if step in ("serve", "reset"):
            write(path, {"sid": context.sid, "initial": {"records": []}, "current": {"records": []}})
        if step == "reset":
            self.run("launch", context)
        if step == "launch":
            if read(path)["sid"] != context.sid:
                raise RuntimeError("launch has stale app session")
            write(context.runtime / "controller" / "fake-launch.json", {"sid": context.sid})
        return context.artifact(
            _artifact_name(step), {"ok": True, "sid": context.sid, "simulation_only": True}
        )


@contextlib.contextmanager
def _deadline(seconds):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("controller deadlines require the main thread")
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise RuntimeError("controller cannot replace an active process timer")

    def expired(*_):
        raise TimeoutError("step seconds budget exhausted")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# What a skipped step was waiting for, named so the row can say so instead of leaving a reader to
# infer it from a token. Both are ``unmeasured``: neither is a statement about the world. The
# operator declining the teacher says nothing about whether the task is feasible, and a launch the
# teacher's verdict cancelled never booted a VM, so it proved nothing either way about the workers.
SKIP_MISSING = {
    "no_teacher": "the teacher rollout, which the operator declined with --no-teacher",
    "teacher_did_not_pass": "a teacher pass, which a launch and a worker episode are built on",
}


def _skip(row, reason):
    """Mark a step done without running it, and say so on the row as well as inside the result.

    ``status`` stays "done" on purpose. The resume loop reads that field to decide what to re-run,
    so a skipped step with any other status would be re-attempted -- and re-skipped -- on every
    re-entry, and ``_policy_run`` reads it too, to find the grade row whose reason it reports to the
    agreement. What was missing was a statement on the row itself: the only signs of a skip were a
    ``result`` a reader had to look inside and a ``started`` left at None, so anything that asked
    "is this step done" was told yes.

    What the row now also carries is ``outcome: unmeasured`` -- and the same word inside the result,
    with ``ok: None`` -- which is the shared vocabulary's answer to this exact shape. ``reason`` is
    still the bare token (``no_teacher``, ``teacher_did_not_pass``) because ``BLOCKING_SKIPS`` below
    matches on it and ``_policy_run`` reports it to the agreement; ``missing`` is where the sentence
    a human wants went.

    Traced on 2026-09-10, nothing downstream was fooled into counting a skipped run as a completed
    one: the agreement's ``vm_verified`` requires TEACHER.json's class to be ``teacher_passed``,
    ``difficulty`` counts only runs with a pass or a fail and labels nothing otherwise,
    ``_policy_run`` already reports ``passed: None`` with the skip's reason, the cohort report takes
    ``done`` from the agreement, ``report.py`` never reads a checkpoint at all, and the batch driver
    treats a finished run as a finished run by design (see its controller_done). One reader was
    fooled, and it is the one this flag exists for: see ``_completed``.
    """
    skipped = Receipt.unmeasured(
        (SKIP_MISSING.get(reason, reason),), "this step", reason=reason, skipped=True
    )
    row.update(status="done", outcome=UNMEASURED, finished=now(), skipped=True, result=skipped.body)


# The skip that stopped the work after it as well as its own: the teacher's verdict cancelled the
# launch, so no VM booted and nothing a later step could run on exists. The no-teacher skip is the
# other kind -- the operator declined the safeguard and the launch, the episodes and the grades all
# ran -- so it is not a reason to refuse a restart at any of them.
BLOCKING_SKIPS = ("teacher_did_not_pass",)


def _completed(row):
    """Whether a step row may be restarted past: work that ran, or a skip that stopped nothing.

    Only that one question needs the distinction, and for it a launch that never booted a VM is not
    a prerequisite anything can run on top of. Measured 2026-09-10 on a run whose teacher failed:
    ``--from episode:own`` was accepted past the skipped launch, re-skipped the episode and the grade
    on the teacher's standing verdict, ran ``teardown`` alone and answered ``status: done``. An
    operator asking for the episode again was told the run had finished and never told why it had not
    run.
    """
    if row["status"] != "done":
        return False
    return not (row.get("skipped") and (row.get("result") or {}).get("reason") in BLOCKING_SKIPS)


def _why_incomplete(row):
    """How a row reads when a restart may not begin after it: the skip's reason, or its status."""
    return f"skipped: {(row.get('result') or {}).get('reason')}" if row.get("skipped") else row["status"]


def _execute(root, folder, state, steps, step):
    row = state["steps"][step]
    if row["status"] != "pending":
        state["history"].append({"step": step, **copy.deepcopy(row)})
    row.clear()
    row.update(_pending(), status="running", started=now(), attempt=uuid.uuid4().hex)
    state.update(status="running", responsible_step=step)
    seconds = state["budgets"]["seconds"][step]
    context = Context(root, folder, state, step, time.monotonic() + seconds)
    context.save()
    try:
        with _deadline(seconds):
            receipt = steps.run(step, context)
            context.remaining()
            if not isinstance(receipt, dict):
                raise TypeError("step must return a JSON object")
            json.dumps(receipt, allow_nan=False)
            row["result"] = receipt
            # The step's own word for what it learned, read through the shared reader so a step that
            # still writes one of the older vocabularies is understood anyway. ``PASSED`` is the
            # default only for a receipt that says nothing either way *and* survives the rejection
            # check below: the step ran to completion and did not refuse its own result.
            row["outcome"] = outcome_of(receipt, PASSED)
            # A grade is a measurement: an episode that did not pass is a valid result the
            # teacher step classifies. Every other step must succeed on its own terms.
            flags = ("ok", "accepted") if _kind(step) == "grade" else ("ok", "passed", "accepted")
            if any(receipt.get(key) is False for key in flags) or receipt.get("status") in (
                "failed",
                "error",
                "seeded_review_failed",
            ):
                raise RuntimeError(f"step rejected its result: {receipt}")
            row.update(status="done", finished=now())
            context.save()
    except BaseException as exc:
        # A failed step's outcome is its receipt's when it produced one -- a refused result is a
        # verdict about this world and re-running it reaches the same answer -- and otherwise the one
        # the exception's own type names: ValueError is a verdict, everything else is a fault about
        # the environment and says nothing about the world. ``status`` is unchanged at "failed", so
        # the resume loop re-runs the step exactly as it always did; what the row gains is whether
        # re-running it can possibly help.
        outcome = outcome_of(row.get("result"), None) or Receipt.from_exception(exc, step).outcome
        row.update(status="failed", outcome=outcome, finished=now(), error=f"{type(exc).__name__}: {exc}")
        state.update(status="failed", responsible_step=step)
        context.save()
        if not isinstance(exc, Exception):
            raise
        raise ControllerError(step, row["error"]) from exc


def run_company(
    root,
    folder,
    task_id,
    *,
    budgets,
    backend="fake",
    base_image=None,
    browser_dir=None,
    host_ip=None,
    dry_run=True,
    steps: Steps | None = None,
    from_step=None,
    reset=False,
    no_teacher=False,
    teacher_model=None,
    policies=None,
):
    """Run or resume, or reset to a fresh session and return with VMs launched.

    ``policies`` are the no-hint worker policies that each get an episode and a grade after
    the teacher passes (default: ``[worker_policy] policies`` in config.toml). Every policy
    after the first runs in a fresh session: a new sid and the journaled reset step.
    ``from_step`` implements CLI ``--from``; it invalidates only that suffix. A plain
    "episode"/"grade" means the first configured policy's step.
    ``reset=True`` keeps seed/check, runs a journaled reset, then leaves the policy
    steps and teardown pending. A plain subsequent call continues the episodes.
    A reset interrupted midway resumes using the same newly reserved sid.
    Budgets/options/policies are pinned per task. Each task has its own runtime
    (``task_runtime``), so the tasks of one company run independently, one call each.
    """
    root, folder = Path(root).resolve(), Path(folder).resolve()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", task_id):
        raise ValueError("invalid task_id")
    if backend not in ("fake", "mypcbench"):
        raise ValueError("backend must be fake or mypcbench")
    policies = _policies(policies) if policies is not None else configured_policies(root)
    names = step_names(policies)
    if from_step in POLICY_STEPS:
        from_step = f"{from_step}:{policies[0]}"
    if from_step not in (None, *names) or (from_step and reset):
        raise ValueError("use one of from_step (a controller step) or reset")
    budgets = _budgets(budgets, names)
    runtime = folder / "runtime"
    # Do not follow writable output symlinks into company source or another job.
    # App builds under hub-cache carry node_modules/.bin symlinks of their own; those are not
    # writable outputs and stay out of this check.
    if runtime.is_symlink() or any(
        p.is_symlink()
        for p in runtime.rglob("*")
        if "hub-cache" not in p.parts and "node_modules" not in p.parts
    ):
        raise ValueError("controller runtime must not contain symlinks")
    options = {
        "backend": backend,
        "base_image": str(Path(base_image).resolve()) if base_image else None,
        "browser_dir": str(Path(browser_dir).resolve()) if browser_dir else None,
        "host_ip": host_ip,
        "dry_run": dry_run,
        "no_teacher": no_teacher,
        "teacher_model": teacher_model,
        "policies": policies,
    }
    steps = steps if steps is not None else DefaultSteps()
    task_dir = task_runtime(folder, task_id)
    with run_lock(task_dir / "controller"):
        path = task_dir / "CONTROLLER.json"
        if path.exists():
            state = read(path)
            # Upgrade old checkpoints without replaying any completed step.
            state["options"].setdefault("no_teacher", False)
            state["options"].setdefault("teacher_model", None)
            state["options"].setdefault("policies", list(DEFAULT_POLICIES))
            _migrate_steps(state)
            state["budgets"] = _budgets(state["budgets"], step_names(state["options"]["policies"]))
            state["steps"].setdefault("teacher", _pending())
            state.setdefault("sessions", {})
            state.setdefault("policy_runs", {})
            if state["task_id"] != task_id or state["options"] != options or state["budgets"] != budgets:
                raise ValueError("resume requires the same task, options and budgets")
        else:
            if reset or from_step:
                raise ValueError("restart/reset requires an existing controller")
            state = {
                "version": 1,
                "task_id": task_id,
                "source": str(folder),
                "options": options,
                "budgets": budgets,
                "model_calls_used": 0,
                "sid": f"episode-{uuid.uuid4().hex}",
                "attempt": uuid.uuid4().hex,
                "status": "pending",
                "responsible_step": None,
                "steps": {step: _pending() for step in names},
                "history": [],
                "reset_pending": False,
                "sessions": {},  # policy -> the session it ran (or runs) in
                "policy_runs": {},
            }
        if (
            not from_step
            and not reset
            and not state["reset_pending"]
            and not _reset_in_flight(state)
            and state["status"] != "done"
            and state["options"].get("backend") == "mypcbench"
            and not state["options"].get("dry_run")
            and state["steps"]["serve"]["status"] == "done"
            and not _service_alive(folder, state)
        ):
            # Resuming after the served apps went away (a teardown, a reboot, a killed run):
            # re-serve and re-launch rather than fail every later step with "not live".
            from_step = "serve"
        for step in names:
            state["steps"].setdefault(step, _pending())
        if from_step:
            index = names.index(from_step)
            if blocking := [s for s in names[:index] if not _completed(state["steps"][s])]:
                raise ValueError(
                    "cannot restart past an incomplete prerequisite: "
                    + ", ".join(f"{s} ({_why_incomplete(state['steps'][s])})" for s in blocking)
                )
            if state["reset_pending"]:
                raise ValueError("resume the pending reset first")
            for step in names[index:]:
                state["history"].append({"step": step, **copy.deepcopy(state["steps"][step])})
                state["steps"][step] = _pending()
                if _kind(step) == "episode":
                    # A redone episode gets a fresh session again, unless it is the first to
                    # run on the current one (a plain --from episode keeps that session).
                    state["sessions"].pop(_policy_of(step), None)
            state["attempt"] = uuid.uuid4().hex
        if reset and not state["reset_pending"]:
            if any(state["steps"][s]["status"] != "done" for s in ("load", "check", "serve")):
                raise ValueError("reset requires a checked and served initial world")
            state["history"].append({"reset_from_sid": state["sid"], "steps": copy.deepcopy(state["steps"])})
            state.update(
                sid=f"episode-{uuid.uuid4().hex}", attempt=uuid.uuid4().hex, reset_pending=True, sessions={}
            )
            for step in (*names[names.index("serve") :], "reset"):
                state["steps"][step] = _pending()
        _record_policy_runs(state)
        write(path, state)
        if state["reset_pending"]:
            # The reset receipt may have committed before the coordinator died
            # while finalizing serve/launch. Do not repeat its teardown/launch.
            if state["steps"]["reset"]["status"] != "done":
                _execute(root, folder, state, steps, "reset")
            for step in ("serve", "launch"):
                state["steps"][step] = copy.deepcopy(state["steps"]["reset"])
            state.update(reset_pending=False, status="ready", responsible_step=None)
            write(path, state)
            return state
        for step in names:
            if state["steps"][step]["status"] != "done":
                if step == "teacher" and no_teacher:
                    _skip(state["steps"][step], "no_teacher")
                    write(path, state)
                    continue
                if (step == "launch" or _kind(step) in POLICY_STEPS) and _teacher_did_not_pass(state):
                    _skip(state["steps"][step], "teacher_did_not_pass")
                    _record_policy_runs(state)
                    write(path, state)
                    continue
                if _kind(step) == "episode":
                    _fresh_session(root, folder, state, steps, _policy_of(step), path)
                try:
                    _execute(root, folder, state, steps, step)
                finally:
                    if _kind(step) in POLICY_STEPS:  # a failed episode is a recorded fault
                        _record_policy_runs(state)
                        write(path, state)
        state.update(status="done", responsible_step=None)
        write(path, state)
        return state


def _migrate_steps(state):
    """Rename steps of checkpoints written before this layout, without replaying any of them:
    "seed" became "load"; the single "episode"/"grade" pair became the first policy's pair."""
    steps = state["steps"]
    if "seed" in steps and "load" not in steps:
        steps["load"] = steps.pop("seed")
    first = state["options"]["policies"][0]
    for kind in POLICY_STEPS:
        if kind in steps and f"{kind}:{first}" not in steps:
            steps[f"{kind}:{first}"] = steps.pop(kind)
    for row in state.get("history", []):
        if row.get("step") in POLICY_STEPS:
            row["step"] = f"{row['step']}:{first}"


def _reset_in_flight(state):
    """A fresh-session reset for a later policy was reserved (new sid) but has not completed."""
    row = state["steps"].get("reset")
    return row is not None and row["status"] != "done"


def _fresh_session(root, folder, state, steps, policy, path):
    """Give every policy after the first its own session: reserve a new sid, then run the
    journaled reset step (stop workers, restore the initial apps under the new sid, relaunch)
    so the policy starts from the initial world and not from the previous policy's leftovers.
    The reservation and the reset row are one write, so an interrupted reset resumes with the
    same sid instead of reserving another."""
    sessions = state["sessions"]
    if policy not in sessions:
        if sessions:
            previous = state["steps"].get("reset")
            if previous and previous["status"] != "pending":
                state["history"].append({"step": "reset", **copy.deepcopy(previous)})
            state["history"].append({"reset_from_sid": state["sid"], "fresh_session_for": policy})
            state["sid"] = f"episode-{uuid.uuid4().hex}"
            state["steps"]["reset"] = _pending()
            sessions[policy] = {"sid": state["sid"], "reset": True}
        else:
            sessions[policy] = {"sid": state["sid"], "reset": False}
        write(path, state)
    if sessions[policy]["reset"] and state["steps"]["reset"]["status"] != "done":
        _execute(root, folder, state, steps, "reset")


def _policy_run(state, policy):
    """Per-policy summary from the checkpoint: grade score, passed flag, and whether the run hit
    an environment-class outcome (a wiped collection, an unavailable judge, an episode that
    ended in error or a failed episode step) rather than an ordinary result."""
    episode = state["steps"].get(f"episode:{policy}") or {}
    grade = state["steps"].get(f"grade:{policy}") or {}
    faults = []
    if episode.get("status") == "failed":
        faults.append(f"episode_failed: {episode.get('error')}")
    elif grade.get("status") != "done":
        return None
    report = grade.get("result") or {}
    outcome = (episode.get("result") or {}).get("result")
    if isinstance(outcome, dict) and outcome.get("reason") == "error":
        faults.append("episode_error")
    if report.get("wiped_collections"):
        faults.append("wiped_collections")
    if report.get("judge_unavailable"):
        faults.append("judge_unavailable")
    if faults:
        reason = "; ".join(faults)
    elif report.get("skipped") or report.get("graded") is False:
        reason = report.get("reason")
    else:
        reason = "graded"
    return {
        "score": report.get("score"),
        "passed": report.get("passed"),
        "environment_fault": bool(faults),
        "reason": reason,
    }


def _record_policy_runs(state):
    """state["policy_runs"] is derived from the step rows, so a redo or a reset never leaves a
    stale summary behind."""
    runs = ((policy, _policy_run(state, policy)) for policy in state["options"]["policies"])
    state["policy_runs"] = {policy: run for policy, run in runs if run is not None}


class _MeteredModels:
    """Model access for controller steps. budgeted=True charges the episode's model-call budget
    (the worker policy); budgeted=False (calibration, grading) only inherits the deadline, so a
    team that spends its whole budget working is still graded."""

    def __init__(self, context, *, budgeted=True):
        from company_envs.models import Models

        self.context = context
        self.budgeted = budgeted
        config = load_config(context.root)
        for job in ("world_states", "world_review", "task_grader", "task_judgment"):
            config["models"][job] = config["models"].get(job, config["models"]["expand"])[:1]
        config.setdefault("generation", {})["completion_timeout_seconds"] = context.remaining()
        self.models = Models(config, context.runtime / "controller" / "models")
        self.lock = threading.Lock()

    def call(self, *args, **kwargs):
        # seed_world uses a thread pool: serialize model calls and deadline configuration.
        with self.lock:
            self.models.config["generation"]["completion_timeout_seconds"] = self.context.remaining()
            if not self.budgeted:
                return self.models.call(*args, **kwargs)
            return self.context.model_call(self.models.call, *args, **kwargs)


class DefaultSteps:
    """Real function wiring, with an offline planning branch for dry_run."""

    def run(self, step, context):
        # "episode:<policy>" runs episode(); the step's policy is context.policy.
        result = getattr(self, _kind(step))(context)
        return context.artifact(_artifact_name(step), result)

    def load(self, ctx):
        from .hub_world import load_world
        from .state_seed import seed_world

        # A completed snapshot is reused after a crash; no source changes leak in.
        if ctx.work.exists() and not (ctx.work / "SNAPSHOT.json").exists():
            shutil.rmtree(ctx.work)
        if not ctx.work.exists():
            ctx.work.mkdir(parents=True)
            sources = [
                ctx.folder / p for p in ("MANIFEST.json", "company.json", "apps.json", "tasks", "world")
            ]
            for source in sources:
                if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
                    raise ValueError(f"input symlink: {source}")
                target = ctx.work / source.name
                if source.is_dir():
                    shutil.copytree(source, target)
                elif source.exists():
                    shutil.copy2(source, target)
            write(ctx.work / "SNAPSHOT.json", {"source": str(ctx.folder)})
        manifest = read(ctx.work / "apps.json")
        # Validate paths before any seed writer or hub loader can use them.
        for app in manifest["apps"]:
            _inside(ctx.work, app["state_file"])
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", app["app_id"]):
                raise ValueError("invalid app_id")
        _inside(ctx.work, manifest.get("identities_file", "world/identities.json"))
        if not all((ctx.work / app["state_file"]).is_file() for app in manifest["apps"]):
            if ctx.dry_run:
                raise ValueError("dry run requires an existing seeded world")
            result = seed_world(
                ctx.root, ctx.work, timeout_seconds=ctx.remaining(), models=_MeteredModels(ctx)
            )
            if result["status"] != "seeded_reviewed":
                raise RuntimeError(f"seed review rejected: {result}")
        load_world(ctx.work, ctx.root)
        return {"ok": True, "folder": str(ctx.work), "reused_initial_world": True}

    def check(self, ctx):
        from .hub_world import load_world
        from .world_check import check_folder

        plan = load_world(ctx.work, ctx.root)
        workflow = read(ctx.work / "tasks" / ctx.state["task_id"] / "workflow.json")
        workers = read(ctx.work / "company.json")["workers"]
        roster = workflow["worker_ids"]
        boss = workflow.get("manager_id")
        known = {w["id"] for w in workers}
        if (
            not boss
            or boss not in known
            or not roster
            or set(roster) - known
            or len(set(roster)) != len(roster)
        ):
            raise ValueError("task needs an explicit known manager_id and a valid worker roster")
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]+", w) for w in {*roster, boss}):
            raise ValueError("invalid worker identifier")
        worker_apps = read(ctx.work / "world/worker_apps.json")
        for worker in {*roster, boss}:
            if worker not in worker_apps or not (ctx.work / "world/materials" / worker).is_dir():
                raise ValueError(f"missing worker apps/materials: {worker}")
        read(ctx.work / "world/SEED.json")["reference_date"]  # a served world is a dated world
        result = check_folder(ctx.root, ctx.work, states={p["app_id"]: p["state"] for p in plan})
        write(ctx.work / "runtime/CHECKS.json", result)
        return result

    def serve(self, ctx):
        from .hub_world import load_world

        if ctx.dry_run:
            apps = {}
            workers = read(ctx.work / "apps.json")["workers"]
            for index, item in enumerate(load_world(ctx.work, ctx.root)):
                apps[item["app_id"]] = {
                    "harness_url": f"http://127.0.0.1:{10000 + index}",
                    "workers": {
                        worker: f"http://192.0.2.1:{20000 + index * len(workers) + i}/?sid={ctx.sid}"
                        for i, worker in enumerate(workers)
                    },
                }
            write(ctx.work / "runtime/endpoints.json", {"apps": apps, "dry_run": True})
            write(ctx.work / "runtime/sessions.json", {"sid": ctx.sid, "workers": workers, "dry_run": True})
            return {"ok": True, "sid": ctx.sid, "simulation_only": True}
        return _serve(ctx)

    def launch(self, ctx):
        from .hub_vm import launch_company, reset_company, stop_company

        options = ctx.state["options"]
        if options["backend"] == "mypcbench":
            if not all(options[k] for k in ("base_image", "browser_dir", "host_ip")):
                raise ValueError("mypcbench requires base_image, browser_dir and host_ip")
            if ctx.dry_run:
                raise ValueError("mypcbench requires dry_run=False (--no-dry-run)")
            if ctx.state["model_calls_used"] >= ctx.state["budgets"]["model_calls"]:
                raise ValueError("mypcbench requires a positive remaining model_calls budget")
        _live(ctx)
        if not ctx.dry_run and not all(options[k] for k in ("base_image", "browser_dir", "host_ip")):
            raise ValueError("live launch requires base_image, browser_dir and host_ip")
        if (ctx.work / "runtime/vms/LAUNCH.json").exists():
            stop_company(ctx.work)
            _archive_episode(ctx)
            return reset_company(ctx.work)
        return launch_company(
            ctx.work,
            ctx.state["task_id"],
            base_image=options["base_image"] or ctx.runtime / "unused.qcow2",
            browser_dir=options["browser_dir"] or ctx.runtime / "unused-browser",
            host_ip=options["host_ip"] or "192.0.2.1",
            dry_run=ctx.dry_run,
            workers=_roster(ctx)[0],
        )

    def episode(self, ctx):
        from .harness import Action, Budget, Episode, FakeBackend, WorkerAgent

        if ctx.state["options"]["backend"] == "mypcbench":
            return self._mypcbench_episode(ctx)
        _live(ctx)
        _archive_episode(ctx, ctx.policy)
        roster, boss = _roster(ctx)
        brief = read(ctx.work / "tasks" / ctx.state["task_id"] / "assignment.json")["brief"]
        titles = {w["id"]: w.get("title", w["id"]) for w in read(ctx.work / "company.json")["workers"]}
        actions = iter(
            [
                Action("send_message", {"recipient": w, "text": f"Inspect your apps as {titles[w]}"})
                for w in roster
                if w != boss
            ]
            + [Action("done")]
        )
        seconds = ctx.remaining()
        workers = [
            WorkerAgent(
                boss, titles[boss], lambda obs: next(actions), FakeBackend(), Budget(len(roster), seconds)
            )
        ]
        workers += [
            WorkerAgent(
                w,
                titles[w],
                lambda obs: Action("done"),
                FakeBackend(),
                Budget(1, seconds),
                start_on_message=True,
            )
            for w in roster
            if w != boss
        ]
        result = asyncio.run(
            Episode(workers, boss_id=boss, brief=brief, runtime=ctx.work / "runtime", seconds=seconds).run()
        )
        if result["reason"] not in ("all_done", "budget_exhausted"):
            raise RuntimeError(f"episode did not quiesce successfully: {result}")
        _keep_episode(ctx)
        return {"ok": True, "simulation_only": True, "policy": ctx.policy, "result": result}

    def _mypcbench_episode(self, ctx):
        from .backends.mypcbench import MyPCBenchBackend
        from .harness import Budget, Episode, WorkerAgent
        from .hub_vm import stop_company

        if ctx.dry_run:
            raise ValueError("mypcbench requires dry_run=False (--no-dry-run)")
        policy_name = ctx.policy
        try:
            _live(ctx)
            _archive_episode(ctx, policy_name)
            policy_class = _worker_policy_class(policy_name)
            roster, boss = _roster(ctx)
            brief = read(ctx.work / "tasks" / ctx.state["task_id"] / "assignment.json")["brief"]
            titles = {w["id"]: w.get("title", w["id"]) for w in read(ctx.work / "company.json")["workers"]}
            config = load_config(ctx.root)
            settings = config.get("worker_policy", {})
            # Leave time to export files and stop VMs before grading.
            remaining = ctx.remaining()
            seconds = remaining - min(30, remaining / 4)
            budget = Budget(settings.get("actions", 100), min(settings.get("seconds", 300), seconds))
            workers = []
            for worker in roster:
                directory = ctx.work / "runtime/vms" / worker
                note = directory / "guest/Desktop/ROLE.md"
                policy = policy_class(
                    config,
                    _episode_dir(ctx) / worker / "policy",  # call records survive a VM reset
                    role_note=note.read_text() if note.exists() else titles[worker],
                    reserve=lambda: ctx.model_call(lambda: None),
                )
                workers.append(
                    WorkerAgent(
                        worker, titles[worker], policy, MyPCBenchBackend(directory / "vm.json"), budget
                    )
                )
            result = asyncio.run(
                Episode(
                    workers,
                    boss_id=boss,
                    brief=brief,
                    runtime=ctx.work / "runtime",
                    seconds=seconds,
                    direct_messages=settings.get("direct_messages", True),
                ).run()
            )
            if result["reason"] not in ("all_done", "budget_exhausted"):
                raise RuntimeError(f"episode did not quiesce successfully: {result}")

            async def export():
                deadline = asyncio.get_running_loop().time() + ctx.remaining()
                exports = {}
                for worker in workers:
                    destination = _inside(
                        ctx.work / "runtime/exports",
                        f"{ctx.state['task_id']}/{policy_name}/{worker.worker_id}",
                    )
                    exports[worker.worker_id] = await worker.backend.export_files(
                        destination, deadline=deadline
                    )
                return exports

            exports = asyncio.run(export())
        finally:
            # Also quiesce browser timers and any daemon deliberately detached by
            # guest bash. Failure to stop raises, so grade is never admitted.
            stopped = stop_company(ctx.work)
        _keep_episode(ctx)
        return {
            "ok": True,
            "simulation_only": False,
            "policy": policy_name,
            "result": result,
            "exports": exports,
            "vms": stopped,
        }

    def grade(self, ctx):
        from . import grader

        if ctx.dry_run:
            # No business outcome was graded, which the module docstring has always promised this
            # artifact would say. ``unmeasured`` is that promise in the shared word, and its
            # ``ok: None`` is not a rejection: _execute only stops a step whose result is False.
            return Receipt.unmeasured(
                ("a live app to grade",),
                "the graded episode",
                reason="dry run has no live app evidence",
                graded=False,
                simulation_only=True,
            ).body
        proof = ctx.work / "runtime" / "grades" / ctx.state["task_id"] / "calibration.json"
        # The calibrate step normally leaves the proof here; if a redo or an older runtime lost
        # it, calibrating again is cheaper than a failed step.
        clients, models = _grader_clients(ctx) if proof.is_file() else _calibrated_grader(ctx)
        return grader.grade(ctx.work, ctx.state["task_id"], clients, models=models, label=ctx.policy)

    def calibrate(self, ctx):
        """Author (if needed) and fully calibrate this working copy's grader before any rollout.

        Its own step with its own time budget: judged checks cost three judge votes each, and
        the teacher used to inherit whatever time calibration left over.
        """
        if ctx.dry_run or ctx.state["options"]["backend"] != "mypcbench":
            # Nothing was calibrated, so nothing about this grader was measured. The step is still
            # done and still admits the ones after it -- the grade step calibrates on demand when it
            # finds no proof -- but the row no longer reads like a calibration that happened.
            return Receipt.unmeasured(
                ("live apps to calibrate against",),
                "the grader calibration",
                reason="calibration runs with the live apps only",
                skipped=True,
            ).body
        _calibrated_grader(ctx)
        return Receipt.passed(calibrated=True).body

    def teacher(self, ctx):
        from .teacher import teacher_rollout

        if ctx.dry_run:
            # The teacher is the feasibility proof and a dry run does not take it. Recording that as
            # a pass is how a company with no proof at all reaches the stages that depend on one.
            return Receipt.unmeasured(
                ("a live app for the teacher to work in",),
                "the feasibility proof",
                reason="dry run has no live app evidence",
                skipped=True,
                simulation_only=True,
            ).body
        # A teacher may have persisted a terminal discard before the controller
        # checkpoint was interrupted. Recover that receipt without another episode.
        task_id = ctx.state["task_id"]
        status_path = ctx.work / "tasks" / task_id / "STATUS.json"
        report_ref = f"runtime/teacher/{task_id}/TEACHER.json"
        report_path = ctx.work / report_ref
        if status_path.is_file() and report_path.is_file():
            status, report = read(status_path), read(report_path)
            if (
                status.get("status") == "discarded"
                and status.get("teacher_report") == report_ref
                and report.get("task_id") == task_id
                and report.get("class") in PERSISTENT_DISCARDS
                and status.get("teacher_class") == report["class"]
                and status.get("teacher_score") == report.get("score")
            ):
                return report
        result = teacher_rollout(
            ctx.root,
            ctx.work,
            ctx.state["task_id"],
            backend=ctx.state["options"]["backend"],
            # The teacher is the feasibility proof, so it gets the task's full model-call
            # budget of its own rather than whatever the graded episode left over (which was
            # zero to a few dozen calls and ended every rollout at "budget exhausted").
            budgets={
                "seconds": ctx.remaining() * 0.95,
                "model_calls": ctx.state["budgets"]["model_calls"],
                "teacher_model": ctx.state["options"].get("teacher_model"),
            },
            launch_inputs={k: ctx.state["options"].get(k) for k in ("base_image", "browser_dir", "host_ip")},
        )
        # A safeguard classification is a completed step, even for a discard or
        # a low teacher score. Teardown still runs; bundling enforces STATUS.
        if result.get("class") == "judge_unavailable":
            raise RuntimeError("teacher grade: judge unavailable after retries; rerun the teacher step")
        if result.get("class") == "model_unavailable":
            raise RuntimeError(
                f"teacher rollout: model provider failed ({result.get('reason')}); rerun the teacher step"
            )
        reason = result.get("reason")
        signals = reason.get("unverified_environment") if isinstance(reason, dict) else None
        if (
            result.get("class") == "teacher_failed"
            and _near_miss(result)
            and ctx.state.get("teacher_near_miss_retries", 0) < 1
        ):
            # Six of seven checks with the answer key and the clock ran out: the task is almost
            # certainly feasible, so one more rollout is cheaper than discarding it (CUA-Gym also
            # uses several teacher rollouts). One extra rollout per reason, not one shared between
            # the two: sharing it meant the company this rule was written for had already spent the
            # counter on an environment report, so the near-miss branch was unreachable even on a
            # resume -- its checkpoint carries teacher_env_retries 1 and a score of 0.9.
            ctx.state["teacher_near_miss_retries"] = 1
            ctx.save()
            raise RuntimeError(
                f"teacher scored {result.get('score')} and ran out of time; retrying the rollout once"
            )
        if (
            result.get("class") == "teacher_failed"
            and signals
            and ctx.state.get("teacher_env_retries", 0) < 1
        ):
            # Workers' own environment reports are not trusted enough to discard a task, but a
            # failure that came with them gets one more rollout on a fresh step budget before the
            # failure counts (trex: two workers found the Tableau mock stuck on its loading screen).
            ctx.state["teacher_env_retries"] = 1
            ctx.save()
            raise RuntimeError(
                f"teacher failed while {len(signals)} worker(s) reported environment errors; retrying once"
            )
        return result

    def teardown(self, ctx):
        from .hub_vm import stop_company

        stopped = stop_company(ctx.work)
        _stop_service(ctx, require_reset=True)
        # Overlays are gigabytes per worker and only matter for a reset; traces, screens,
        # guest payloads and exports stay.
        dropped = 0
        for pattern in ("runtime/vms/*/worker.qcow2", "runtime/teacher/**/worker.qcow2"):
            for overlay in ctx.work.glob(pattern):
                overlay.unlink()
                dropped += 1
        for cache in ctx.work.glob("runtime/teacher/*/runs/*/company/runtime/hub-cache"):
            shutil.rmtree(cache, ignore_errors=True)  # app builds copied per teacher run
        return {
            "ok": True,
            "vms": stopped,
            "apps": "reset_and_stopped",
            "overlays_dropped": dropped,
            "dry_run": ctx.dry_run,
        }

    def reset(self, ctx):
        from .hub_vm import stop_company

        stop_company(ctx.work)
        _archive_episode(ctx)
        _stop_service(ctx)
        served = self.serve(ctx)  # New sid from controller; immutable initial snapshot.
        launched = self.launch(ctx)
        return {"ok": True, "sid": ctx.sid, "serve": served, "launch": launched}


# Only a systemic defect (a missing record, a permission wall, an absent feature) describes the
# task itself and is final until repaired. Environment errors are infrastructure (an SSH timeout,
# a wiped app) and grader errors are usually our bug: both are rolled out again on the next run.
PERSISTENT_DISCARDS = {"systemic_defect"}


def _grader_clients(ctx):
    """Clients for the served apps and an unbudgeted judge for this working copy."""
    from .hub_app import HubClient

    _live(ctx)
    endpoints = read(ctx.work / "runtime/endpoints.json")["apps"]
    clients = {app: HubClient(entry["harness_url"]) for app, entry in endpoints.items()}
    return clients, _MeteredModels(ctx, budgeted=False)


def _calibrated_grader(ctx):
    """Clients and judge, with this working copy's grader authored if missing and fully calibrated."""
    from . import grader

    clients, models = _grader_clients(ctx)
    task = ctx.state["task_id"]
    task_dir = ctx.work / "tasks" / task
    if not (task_dir / "grader.json").is_file():
        grader.author_grader(ctx.root, ctx.work, task, models=models)
    # A task calibrated by golden replay has no reference.json; replay the golden again here.
    golden = (task_dir / "golden.json").is_file() and not (task_dir / "reference.json").is_file()
    grader.calibrate(ctx.work, task, clients, golden=golden, models=models)
    return clients, models


NEAR_MISS_SCORE = 0.8


def _near_miss(result):
    """A high teacher score whose episode ended on the time or action budget, not on a verdict."""
    score = result.get("score")
    episode = (result.get("trajectory_summary") or {}).get("episode") or {}
    return (
        isinstance(score, (int, float))
        and score >= NEAR_MISS_SCORE
        and episode.get("reason") in ("budget_exhausted", "episode_time_budget")
    )


def _teacher_did_not_pass(state):
    """True when a real teacher rollout ran and its class is anything but teacher_passed."""
    result = state["steps"]["teacher"].get("result") or {}
    return "class" in result and result["class"] != "teacher_passed"


def _inside(base, relative):
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()) or Path(relative).is_absolute():
        raise ValueError(f"path escapes working copy: {relative}")
    return path


def _roster(ctx):
    task = read(ctx.work / "tasks" / ctx.state["task_id"] / "workflow.json")
    return list(dict.fromkeys([task["manager_id"], *task["worker_ids"]])), task["manager_id"]


KEEP_ACROSS_EPISODES = (
    "calibration.json",
    "judge_bench.json",
)  # proofs of the initial world, not of an episode
PER_POLICY_DIRS = ("exports", "grades", "episodes")  # runtime/<dir>/<task>/<policy>/
EPISODE_FILES = ("episode.json", "episode-result.json", "messages.jsonl")


def _worker_policy_class(name):
    """Our own WorkerPolicy, or company_envs.world.policies.<name>.<Name>Policy, imported only when
    selected so a missing adapter never affects the other policies. Adapters take WorkerPolicy's
    constructor (config, directory, *, role_note, models, reserve) and are awaited per observation."""
    if name == DEFAULT_POLICIES[0]:
        from .backends.mypcbench import WorkerPolicy

        return WorkerPolicy
    module = importlib.import_module(f"company_envs.world.policies.{name}")
    return getattr(module, "".join(part.capitalize() for part in name.split("_")) + "Policy")


def _episode_dir(ctx):
    return _inside(ctx.work / "runtime/episodes", f"{ctx.state['task_id']}/{ctx.policy}")


def _keep_episode(ctx):
    """File the finished episode under runtime/episodes/<task>/<policy>/: the harness receipts and
    every worker's trace. A later policy's reset would otherwise discard the traces with the VMs."""
    runtime = ctx.work / "runtime"
    target = _episode_dir(ctx)
    moves = [(runtime / name, target / name) for name in EPISODE_FILES]
    moves.extend((trace, target / trace.parent.name / "trace") for trace in (runtime / "vms").glob("*/trace"))
    for source, destination in moves:
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), destination)


def _archive_episode(ctx, policy=None):
    """Set a previous episode's artifacts aside: the current session's harness receipts and VM
    traces, and with ``policy`` that policy's own exports, grade and filed episode. Other
    policies' directories stay. Calibration proofs belong to the initial world and stay: the
    calibrate step runs before the teacher and the episodes, and grade requires the proof."""
    runtime = ctx.work / "runtime"
    task = ctx.state["task_id"]
    paths = [runtime / name for name in EPISODE_FILES]
    paths.extend((runtime / "vms").glob("*/trace"))
    # Reports written flat under grades/<task>/ by the single-episode layout.
    paths.extend(
        p for p in (runtime / "grades").glob("*/*") if p.is_file() and p.name not in KEEP_ACROSS_EPISODES
    )
    if policy:
        paths.extend(_inside(runtime / kind, f"{task}/{policy}") for kind in PER_POLICY_DIRS)
    archive = ctx.runtime / "controller" / "history" / uuid.uuid4().hex
    for source in paths:
        if source.exists():
            target = archive / source.relative_to(runtime)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), target)


def _service_alive(folder, state):
    """Whether the hub process recorded by serve still exists for this state's session."""
    from .hub_vm import _process_identity

    try:
        receipt = read(task_runtime(folder, state["task_id"]) / "controller/company/runtime/SERVICE.json")
    except (OSError, ValueError, KeyError):
        return False
    try:
        return (
            receipt["sid"] == state["sid"]
            and _process_identity(receipt["process"]["pid"]) == receipt["process"]
        )
    except (KeyError, TypeError):
        return False


def _live(ctx):
    if ctx.dry_run:
        return
    from .hub_app import HubClient
    from .hub_vm import _process_identity

    receipt = read(ctx.work / "runtime/SERVICE.json")
    if receipt["sid"] != ctx.sid or _process_identity(receipt["process"]["pid"]) != receipt["process"]:
        raise RuntimeError("serve prerequisite is no longer live; use --reset or --from serve")
    sessions = read(ctx.work / "runtime/sessions.json")
    if sessions["sid"] != ctx.sid:
        raise RuntimeError("serve prerequisite has a stale sid")
    for entry in read(ctx.work / "runtime/endpoints.json")["apps"].values():
        HubClient(entry["harness_url"]).inspect(ctx.sid)


def _stop_service(ctx, *, require_reset=False):
    if ctx.dry_run:
        return
    from .hub_vm import _stop_process

    path = ctx.work / "runtime/SERVICE.json"
    if path.exists():
        receipt = read(path)
        _stop_process(receipt["process"])
        stopped = ctx.work / "runtime/SERVICE-STOPPED.json"
        if require_reset and (not stopped.exists() or read(stopped) != receipt):
            raise RuntimeError("hub service stopped without a confirmed initial-state reset")
    elif require_reset:
        raise RuntimeError("missing hub service ownership receipt")


def _serve(ctx):
    from .hub_vm import _process_identity

    path = ctx.work / "runtime/SERVICE.json"
    if path.exists():
        receipt = read(path)
        if receipt["sid"] == ctx.sid and _process_identity(receipt["process"]["pid"]) == receipt["process"]:
            # The previous coordinator may have died during the app build.
            while _process_identity(receipt["process"]["pid"]) == receipt["process"]:
                ctx.remaining()
                ready = ctx.work / "runtime/SERVICE-READY.json"
                if ready.exists() and read(ready) == receipt:
                    _live(ctx)
                    return {"ok": True, **receipt}
                time.sleep(min(0.1, ctx.remaining()))
        _stop_service(ctx)
    config = load_config(ctx.root)
    hub_root = Path(config["design"]["hub_root"])
    if not hub_root.is_absolute():
        hub_root = ctx.root / hub_root
    request = ctx.work / "runtime/SERVICE-REQUEST.json"
    write(
        request, {"root": str(ctx.root), "folder": str(ctx.work), "hub_root": str(hub_root), "sid": ctx.sid}
    )
    with (ctx.work / "runtime/service.log").open("a") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", __name__, str(request)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ | {"npm_config_cache": str(ctx.runtime / "controller/npm-cache")},
        )
    # Persist process birth identity before waiting for app build/readiness.
    owner = {"sid": ctx.sid, "process": _process_identity(process.pid)}
    if owner["process"] is None:
        raise RuntimeError("hub service exited before ownership could be recorded")
    write(path, owner)
    try:
        while process.poll() is None:
            ctx.remaining()
            ready = ctx.work / "runtime/SERVICE-READY.json"
            if ready.exists() and read(ready) == owner:
                _live(ctx)
                return {"ok": True, **owner}
            time.sleep(min(0.1, ctx.remaining()))
        raise RuntimeError(f"hub service exited; see {ctx.work / 'runtime/service.log'}")
    except BaseException:
        _stop_service(ctx)
        raise


def _service_main(request):
    """Detached service owner keeps proxies alive across controller invocations."""
    from .hub_vm import _process_identity
    from .hub_world import CompanyWorld

    config = read(request)
    folder = Path(config["folder"])
    owner = {"sid": config["sid"], "process": _process_identity(os.getpid())}
    write(folder / "runtime/SERVICE.json", owner)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    world = CompanyWorld(folder, config["hub_root"], folder / "runtime/hub-cache", root=config["root"])
    world.sid = config["sid"]
    try:
        world.start()
        write(
            folder / "runtime/SERVICE-READY.json",
            {"sid": world.sid, "process": _process_identity(os.getpid())},
        )
        while not stopped.wait(0.5):
            pass
    finally:
        try:
            world.reset()
            write(folder / "runtime/SERVICE-STOPPED.json", owner)
        finally:
            world.stop()


if __name__ == "__main__":
    _service_main(Path(sys.argv[1]))
