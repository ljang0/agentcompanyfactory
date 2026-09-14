"""One teacher episode, isolated from calibration and ordinary worker attempts.

Backends implement prepare(folder, task_id, roster, config), backend(worker),
quiesce(), close(), and expose clients after prepare. Preparation must seed fresh
sessions, verify readback and deliver materials/URLs. quiesce stops guest effects
before grading; close releases services after grading. Supplied models are quick
or async doubles; live worker inference uses WorkerPolicy's cancellable process.

Budgets: seconds (number or controller step map), actions (per worker, default
100), model_calls (shared cap, default 0), threshold (default 1). Optional reserve
adds the controller's persistent reservation. teacher_model overrides config.
Live provisioning uses the source runtime/vms/LAUNCH.json inputs. Its synchronous
launcher has the same remote cancellation limitations as the controller.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import math
import re
import shlex
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from company_envs.config import load_config
from company_envs.storage import now, read, run_lock, write

from . import grader
from .backends.mypcbench import MyPCBenchBackend, WorkerPolicy
from .golden import initial_states
from .harness import Budget, BudgetExhausted, Episode, FakeBackend, WorkerAgent

DISCARD_CLASSES = {"environment_error", "grader_error", "systemic_defect"}
INSIDER_LIMIT = 9000  # characters of reference solution per worker
TEACHER_NOTE = """Complete the assignment using your apps, materials and peers.
If the task is impossible because a promised record is missing, permissions block
required work, or an app lacks the required feature, finish with summary
'SYSTEMIC_DEFECT: missing_record|permission_wall|feature_absent: <specific evidence>'.
For broken VM/app/proxy/material delivery or an unreachable URL, report
'ENVIRONMENT_ERROR: <specific evidence>'. Ordinary unsuccessful work or exhausted
budgets is not an environment defect. Never claim success without verifying writes.
"""
_ENV = re.compile(
    r"environment_error:|err_connection_|err_name_not_resolved|connection refused|"
    r"unreachable (?:url|app)|(?:url|app) (?:is )?unreachable|seed readback mismatch",
    re.IGNORECASE,
)
_DEFECT = re.compile(
    r"^SYSTEMIC_DEFECT:\s*(?:missing_record|permission_wall|feature_absent)\s*:", re.IGNORECASE
)
_CRASH = re.compile(r"^[A-Za-z_]*(?:Error|Exception):")


class _MemoryClient:
    def __init__(self, state):
        self.initial = copy.deepcopy(state)
        self.current = copy.deepcopy(state)

    def inspect(self, sid):
        return {"initial_state": self.initial, "current_state": self.current, "state_diff": {}}


class FakeTeacherBackend:
    """Offline lifecycle; subclass backend() for scripted app effects/failures."""

    simulation_only = True

    def prepare(self, folder, task_id, roster, config):
        self.folder = folder
        self.sid = f"teacher-{uuid.uuid4().hex}"
        self.clients = {app: _MemoryClient(state) for app, state in initial_states(folder).items()}
        write(folder / "runtime/sessions.json", {"sid": self.sid, "workers": roster})

    def launch(self):
        """No VMs offline; the seed check between prepare and launch has nothing to wait for."""

    def backend(self, worker):
        return FakeBackend()

    def quiesce(self):
        pass

    def close(self):
        pass


class _LiveBackend:
    simulation_only = False

    def __init__(self, root, source, launch_inputs=None, work_cache=None, withheld_inputs=None):
        self.root, self.source, self.world, self.folder = root, source, None, None
        self.launch_inputs = launch_inputs  # VM inputs when the worker VMs have not launched yet
        self.work_cache = work_cache
        self.withheld_inputs = withheld_inputs

    def prepare(self, folder, task_id, roster, config):
        from .hub_app import HubClient
        from .hub_world import CompanyWorld
        from .trial_evidence import ObservedWorld

        self.folder = folder
        self.roster = roster
        hub = Path(config["design"]["hub_root"])
        world_class = ObservedWorld if (folder / "world/POPULATION.json").exists() else CompanyWorld
        self.world = world_class(
            folder,
            hub if hub.is_absolute() else self.root / hub,
            self.work_cache or folder / "runtime/hub-cache",
            root=self.root,
            episode=uuid.uuid4().hex,
        )
        if self.withheld_inputs:
            from .trial_access import mask_records, proxy_factory

            self.world.start(proxy_factory=proxy_factory(self.world, self.withheld_inputs))
            worker = self.withheld_inputs["worker_id"]
            refs = self.withheld_inputs["references"]
            for app, entry in self.world.endpoints.items():
                if worker in entry["workers"]:
                    view = HubClient(entry["workers"][worker].split("/?")[0]).current(self.world.sid)[
                        "stored_state"
                    ]
                    if mask_records(view, refs, app) != view:
                        raise ValueError("An ablated input remains visible to the target worker")
            write(
                folder / "runtime/INPUT-ABLATION.json",
                {
                    **self.withheld_inputs,
                    "status": "view_probe_passed",
                    "scope": "Withhold these native input records from this worker. Other workers and desktop files retain their ordinary access; substitutes remain possible and will be reported by the trial.",
                },
            )
        else:
            self.world.start()
        self.clients = {app: HubClient(entry["harness_url"]) for app, entry in self.world.endpoints.items()}
        inputs = self.launch_inputs or read(self.source / "runtime/vms/LAUNCH.json")["inputs"]
        self._launch = (
            (folder, task_id),
            {
                **{k: inputs[k] for k in ("base_image", "browser_dir", "host_ip")},
                "workers": roster,
                "dry_run": False,
            },
        )

    def launch(self):
        """Start the worker VMs. Separate from ``prepare`` because opening an app writes to it:
        a clone merges its own defaults under the state it is handed and saves the result, so a
        seed read back after the browsers are up differs from the seed by that normalisation."""
        from .hub_vm import launch_company

        positional, options = self._launch
        launch_company(*positional, **options)
        if (self.folder / "world/POPULATION.json").exists():
            self.synchronize_clocks()

    def synchronize_clocks(self):
        """Verify the guest wall clock against the dated world before policy dispatch."""
        from .hub_vm import GUEST_PASSWORD

        reference = read(self.folder / "world/POPULATION.json")["reference_date"]
        instant = datetime.fromisoformat(reference)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        target = instant.timestamp()
        script = (
            "import json, subprocess, time\n"
            "before = time.time()\n"
            "subprocess.run(['timedatectl', 'set-ntp', 'false'], check=True, timeout=15)\n"
            f"subprocess.run(['date', '--utc', '--set', '@{target}'], check=True, "
            "stdout=subprocess.DEVNULL, timeout=10)\n"
            "print(json.dumps({'before_epoch': before, 'after_epoch': time.time()}))\n"
        )
        command = f"printf '%s\\n' {shlex.quote(GUEST_PASSWORD)} | sudo -S -p '' python3 -c " + shlex.quote(
            script
        )

        async def synchronize(worker):
            result = await self.backend(worker).transport.run(
                command, deadline=asyncio.get_running_loop().time() + 90
            )
            if result["exit_code"] != 0:
                raise RuntimeError(f"{worker}: guest clock setup failed")
            measured = json.loads(base64.b64decode(result["stdout"]))
            if abs(measured["after_epoch"] - target) > 15:
                raise ValueError(f"{worker}: guest clock does not match the frozen reference")
            return worker, {"status": "pass", **measured}

        async def collect():
            return dict(await asyncio.gather(*(synchronize(worker) for worker in self.roster)))

        write(
            self.folder / "runtime/vms/CLOCK.json",
            {"reference_date": reference, "target_epoch": target, "workers": asyncio.run(collect())},
        )

    def backend(self, worker):
        return MyPCBenchBackend(self.folder / "runtime/vms" / worker / "vm.json")

    def quiesce(self):
        from .hub_vm import stop_company

        if self.folder is not None:
            stop_company(self.folder)
            # Keep actual native evidence even if desktop export failed. Grading still requires
            # complete exports; this snapshot is diagnostic and never substitutes fabricated files.
            states, errors = {}, {}
            sid = read(self.folder / "runtime/sessions.json")["sid"]
            for app, client in getattr(self, "clients", {}).items():
                try:
                    states[app] = client.inspect(sid)
                except Exception as exc:  # noqa: BLE001 -- retain every available app before reset
                    errors[app] = f"{type(exc).__name__}: {exc}"
            write(
                self.folder / "runtime/NATIVE-FINAL.json",
                {
                    "at": now(),
                    "status": "incomplete" if errors else "captured",
                    "apps": states,
                    "errors": errors,
                },
            )
            if errors:
                raise ValueError(f"Native final snapshot failed: {errors}")

    def export(self, task_id, deadline):
        async def collect():
            return dict(
                zip(
                    self.roster,
                    await asyncio.gather(
                        *(
                            self.backend(worker).export_files(
                                self.folder / "runtime/exports" / task_id / worker, deadline=deadline
                            )
                            for worker in self.roster
                        )
                    ),
                )
            )

        result = asyncio.run(collect())
        if any(row.get("status") != "exported" for row in result.values()):
            raise ValueError("A worker desktop export is incomplete")
        write(self.folder / "runtime/exports" / task_id / "EXPORT.json", result)
        return result

    def close(self):
        if self.world is not None:
            self.world.stop()


class _Models:
    def __init__(self, config, directory, supplied, reserve, remaining):
        self.config, self.directory, self.supplied = config, directory, supplied
        self.reserve, self.remaining = reserve, remaining
        self.live = None

    def call(self, job, *args, **kwargs):
        self.reserve()
        if self.supplied is not None:
            return self.supplied.call("teacher" if job == "worker_policy" else job, *args, **kwargs)
        from company_envs.models import Models

        self.config.setdefault("generation", {})["completion_timeout_seconds"] = self.remaining()
        if self.live is None:
            self.live = Models(self.config, self.directory)
        return self.live.call(job, *args, timeout_seconds=self.remaining(), **kwargs)


class _BackendEvidence:
    """Distinguish backend exceptions from policy/action failures in the harness."""

    def __init__(self, backend, worker, failures):
        self.backend, self.worker, self.failures = backend, worker, failures

    async def _call(self, method, *args, **kwargs):
        try:
            return await getattr(self.backend, method)(*args, **kwargs)
        except Exception as exc:
            self.failures.append(f"{self.worker} {method}: {type(exc).__name__}: {exc}")
            raise

    async def observe(self, *, deadline):
        return await self._call("observe", deadline=deadline)

    async def execute(self, action, *, deadline):
        return await self._call("execute", action, deadline=deadline)


def insider_note(work, task_id, worker):
    """The reference solution's steps for one worker, as privileged guidance.

    The teacher proves a task is completable by someone who knows the answer; ordinary
    workers never see this. Content is bounded so a long document cannot crowd out the
    rest of the observation.
    """
    path = work / "tasks" / task_id / "golden.json"
    if not path.is_file():
        return ""
    if (work / "world/POPULATION.json").exists():
        steps = [
            {"sequence": number, **step}
            for number, step in enumerate(read(path), 1)
            if step.get("worker_id") == worker
        ]
        if not steps:
            return ""
        # A clipped long document silently removed decisions, calculations and later handoffs.
        # The feasibility teacher needs the complete reference for its own role.
        return (
            "INSIDER REFERENCE (privileged; ordinary workers never see this). These are all "
            "reference steps assigned to you, in team sequence. Achieve these outcomes using "
            "your own apps and identity. Obtain and consume peers' actual work before dependent "
            "decisions. Use your own prose where appropriate and preserve unrelated records.\n"
            "Reference record IDs and creation timestamps are examples. Use actual app timestamps "
            "and identify peers' delivered work by sender and content, including new IDs created "
            "by their apps. Preserve business deadlines and due dates. Do not wait for a literal "
            "reference ID or filter out real work by an example creation timestamp.\n"
            "State patches describe desired outcomes, not administrative API access. Use normal "
            "app controls and confirm saved content; the administrative set_current route is unavailable.\n"
            + json.dumps(steps, ensure_ascii=False)
        )
    lines = []
    for number, step in enumerate(read(path), 1):
        if step.get("worker_id") != worker:
            continue
        if step.get("action") == "message":
            lines.append(f"Step {number}: tell your peers: {step.get('text', '')}")
            continue
        for collection, patch in (step.get("state_patch") or {}).items():
            records = list(patch.values()) if isinstance(patch, dict) else patch
            if not isinstance(records, list):
                lines.append(
                    f"Step {number}: in {step['app_id']}, set {collection} = {json.dumps(patch)[:600]}"
                )
                continue
            for record in records:
                shown = json.dumps(record, ensure_ascii=False)
                if len(shown) > 1500:
                    shown = shown[:1500] + " ..."
                lines.append(f"Step {number}: in {step['app_id']}, write to {collection}: {shown}")
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > INSIDER_LIMIT:
        text = text[:INSIDER_LIMIT] + "\n..."
    return (
        "INSIDER REFERENCE (privileged; ordinary workers never see this). These are the "
        "reference solution's steps assigned to you. Produce the same outcomes in the apps "
        "through the desktop, using your own wording where the record is prose:\n" + text
    )


def _snapshot(source, work, task_id):
    # A grader needs the private task, but WorkerPolicy receives only the public
    # brief and worker role; the teacher adds the reference solution as an insider note.
    for name in ("MANIFEST.json", "company.json", "apps.json", "world", f"tasks/{task_id}"):
        path, target = source / name, work / name
        if path.is_symlink() or any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError(f"input symlink: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)
    if (source / "world-spec.json").is_file():
        shutil.copy2(source / "world-spec.json", work / "world-spec.json")
    population = work / "world/POPULATION.json"
    if population.is_file():
        # Adapt the accepted Stage 3 handoff for the existing VM clock reader.
        # This metadata exists only in the disposable trial copy, never the source.
        manifest = read(population)
        write(
            work / "world/SEED.json",
            {
                "reference_date": manifest["reference_date"],
                "source": "POPULATION.json",
                "status": "accepted_population_trial_copy",
            },
        )
    proof = source / "runtime/grades" / task_id / "calibration.json"
    if proof.exists():
        write(work / "runtime/grades" / task_id / "calibration.json", read(proof))


def _signals(runtime):
    environment, defects, summary = [], [], {}
    for path in sorted((runtime / "vms").glob("*/trace/events.jsonl")):
        worker = path.parent.parent.name
        actions, failed, done = {}, {}, []
        for number, line in enumerate(path.read_text().splitlines(), 1):
            event = json.loads(line)
            ref = f"{path.relative_to(runtime)}:{number}"
            if event["event"] == "action":
                actions[event["number"]] = event
                if event["name"] == "done":
                    reason = event["arguments"].get("summary", "")
                    done.append(reason)
                    lower = reason.lower()
                    if _ENV.search(reason):
                        environment.append({"worker": worker, "reason": reason, "evidence": ref})
                    elif _DEFECT.match(reason) or (
                        any(s in lower for s in ("impossible", "cannot complete", "can't complete"))
                        and any(
                            s in lower
                            for s in (
                                "missing record",
                                "record is missing",
                                "permission",
                                "feature absent",
                                "feature is unavailable",
                                "feature is not supported",
                            )
                        )
                    ):
                        defects.append({"worker": worker, "reason": reason, "evidence": ref})
            elif event["event"] == "backend_result":
                result = event["result"]
                if _ENV.search(json.dumps(result)):
                    environment.append({"worker": worker, "reason": result, "evidence": ref})
                action = actions[event["number"]]
                key = json.dumps([action["name"], action["arguments"]], sort_keys=True)
                if (
                    result.get("ok") is False
                    or result.get("success") is False
                    or result.get("error")
                    or result.get("exit_code", 0) != 0
                ):
                    failed.setdefault(key, []).append(ref)
                else:
                    failed.pop(key, None)
            elif event["event"] == "screen" and _ENV.search(event.get("text", "")):
                environment.append({"worker": worker, "reason": event["text"], "evidence": ref})
        for action, refs in failed.items():
            if len(refs) >= 3:
                defects.append(
                    {
                        "worker": worker,
                        "reason": "repeated identical failed action",
                        "action": json.loads(action),
                        "evidence": refs,
                    }
                )
        summary[worker] = {
            "actions": len(actions),
            "done_reasons": done,
            "trace": str(path.relative_to(runtime)),
        }
    return environment, defects, summary


_PROVIDER_SIGNS = ("ModelUnavailable", "provider process failed", "usage limit")


def _provider_outage(result):
    """The worker error that names a model-provider failure, if the episode ended on one."""
    for worker in (result or {}).get("workers", {}).values():
        error = str((worker or {}).get("error") or "")
        if any(sign in error for sign in _PROVIDER_SIGNS):
            return error[:300]
    return None


NEAR_MISS_SCORE = 0.8


def retry_reason(classification, score, budget):
    """Why one more rollout could do better than this one, or None when nothing says it would.

    The teacher is the feasibility gate, and a failure it records freezes the task: the controller
    skips every later step and its checkpoint reaches ``done``, which no input of the vm stage can
    make stale. So the receipt has to say, itself, whether the rollout reached a verdict about the
    task or ran out of something. Three ways it did not reach one:

    - ``unfunded``: the rollout had no model calls to spend, so its score is not about the task at
      all. Measured: a rollout launched without ``--model-calls`` booted five VMs, every worker's
      first step hit the cap, and the receipt read ``teacher_failed``, score 0.0, threshold 1.0 --
      character for character what a world the reference solution cannot finish writes.
    - ``worker_fault``: a worker ended on its own fault (a dead SSH transport, a provider that
      stopped answering) while its peers worked on. The grade is of a short-handed team.
    - ``near_miss``: the score is at least NEAR_MISS_SCORE and the episode ended on a budget rather
      than on a verdict. trex-company sat on exactly this, at 0.90 with five workers out of time.
    """
    if classification != "teacher_failed":
        return None
    if not budget.get("model_calls"):
        return "unfunded"
    if budget.get("episode_reason") == "worker_error":
        return "worker_fault"
    near = isinstance(score, (int, float)) and not isinstance(score, bool) and score >= NEAR_MISS_SCORE
    if near and budget.get("episode_reason") == "budget_exhausted":
        return "near_miss"
    return None


def _grade_class(report, apps):
    errors = report.get("errors", {})
    if any(app in errors for app in apps):
        return "environment_error"
    if report.get("wiped_collections"):
        return "environment_error"
    if report.get("judge_unavailable"):
        # The judge transport failed after retries: nothing is known about the task, so this is
        # neither a pass nor a discard; the controller step fails and a rerun tries again.
        return "judge_unavailable"
    if report.get("complete") is False:
        return "verification_incomplete"
    if errors or any(
        row.get("status") in ("error", "crashed") or row.get("error") or _CRASH.match(row.get("reason", ""))
        for row in report.get("checks", [])
    ):
        return "grader_error"
    return None


def teacher_rollout(
    root,
    folder,
    task_id,
    *,
    backend,
    budgets,
    models=None,
    launch_inputs=None,
    trial_kind="teacher",
    unavailable_worker=None,
):
    """Run one fresh attempt, grade, persist exactly one class and disposition.

    Repeated explicit calls preserve earlier attempts under runs/. A discarded
    task stays discarded until repaired explicitly; a later score never clears it.
    Model budget exhaustion is an ordinary bounded teacher failure, not a defect.
    """
    root, folder = Path(root).resolve(), Path(folder).resolve()
    from .task_assessment import require_task_world

    require_task_world(root, folder, task_id)
    task = grader._task(folder, task_id)
    if trial_kind not in {"teacher", "ordinary", "worker-ablation", "input-ablation"}:
        raise ValueError("Unknown trial kind")
    if unavailable_worker is not None and trial_kind != "worker-ablation":
        raise ValueError("A missing worker must be an explicitly labeled ablation")
    seconds = budgets["seconds"]
    seconds = seconds["teacher"] if isinstance(seconds, dict) else seconds
    budget = Budget(budgets.get("actions", 100), seconds)
    limit, threshold = budgets.get("model_calls", 0), budgets.get("threshold", 1.0)
    if type(limit) is not int or limit < 0:
        raise ValueError("model_calls must be a nonnegative integer")
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("threshold must be between zero and one")
    if isinstance(backend, str):
        if backend not in ("fake", "mypcbench"):
            raise ValueError("backend must be fake, mypcbench, or a rollout backend")
        backend = FakeTeacherBackend() if backend == "fake" else _LiveBackend(root, folder, launch_inputs)
    directory = grader._safe_path(folder, f"runtime/{trial_kind}/{task_id}")
    with run_lock(directory):
        attempt = directory / "runs" / uuid.uuid4().hex
        work = attempt / "company"
        runtime = work / "runtime"
        started = time.monotonic()
        deadline, used = started + seconds, 0
        result, report, errors, config = None, None, [], {}
        interrupted = None
        classification, reason, summary = None, None, {}
        backend_failures = []
        reset_proof = {"status": "unmeasured"}

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("teacher seconds budget exhausted")
            return value

        def reserve():
            nonlocal used
            remaining()
            if used >= limit:
                # BudgetExhausted, not a bare RuntimeError: the harness reads it as "this worker is
                # done", where any other exception used to fail the worker, cancel its four peers and
                # end the episode on `error`. It is a RuntimeError subclass, so a caller catching
                # that still catches this.
                raise BudgetExhausted("teacher model call budget exhausted")
            if budgets.get("reserve"):
                budgets["reserve"]()
            used += 1
            write(attempt / "budget.json", {"model_calls_used": used, "model_calls": limit})

        try:
            _snapshot(folder, work, task_id)
            config = load_config(root)
            specs = config["models"].get("teacher", config["models"]["expand"])
            if budgets.get("teacher_model"):
                specs = [budgets["teacher_model"]]
            config["models"]["worker_policy"] = specs[:1]
            config["models"]["task_judgment"] = config["models"].get(
                "task_judgment", config["models"]["expand"]
            )[:1]
            workflow = read(work / "tasks" / task_id / "workflow.json")
            boss = workflow["manager_id"]
            roster = list(dict.fromkeys([boss, *workflow["worker_ids"]]))
            if unavailable_worker is not None:
                if unavailable_worker not in roster or unavailable_worker == boss:
                    raise ValueError("Worker ablation requires a participating specialist")
                roster.remove(unavailable_worker)
            titles = {w["id"]: w.get("title", w["id"]) for w in read(work / "company.json")["workers"]}
            backend.prepare(work, task_id, roster, config)
            sid = read(runtime / "sessions.json")["sid"]
            for app, state in initial_states(work).items():
                seen = backend.clients[app].inspect(sid)
                if seen["initial_state"] != state or seen["current_state"] != state:
                    raise ValueError(f"{app}: seed readback mismatch")
            backend.launch()
            metered = _Models(config, attempt / "models", models, reserve, remaining)
            # Grading has its own allowance: a teacher that spends its whole budget working must
            # still be judged, so judge votes never come out of the policy's call budget.
            grading = _Models(config, attempt / "grading-models", models, remaining, remaining)
            workers = []
            app_coordination = (
                "This trial uses company apps for all coordination; the direct send_message bus is disabled. "
                "The manager should post concrete assignments in shared Slack conversations or Gmail. "
                "Other workers should check those apps for instructions and return their findings there. "
                "Each worker uses only their own identity and authority; outside specialists' decisions "
                "remain pending until actual evidence arrives."
                if (work / "world/POPULATION.json").exists()
                else ""
            )
            for worker in roster:
                policy = WorkerPolicy(
                    config,
                    runtime / "vms" / worker / "policy",
                    role_note="\n\n".join(
                        part
                        for part in (
                            TEACHER_NOTE,
                            app_coordination,
                            insider_note(work, task_id, worker) if trial_kind == "teacher" else "",
                        )
                        if part
                    ),
                    models=metered if models is not None else None,
                    reserve=reserve if models is None else None,
                )

                async def bounded(obs, policy=policy):
                    # Stop at the shared call cap without turning a budget into
                    # a harness exception and discarding an otherwise valid task.
                    from .harness import Action

                    if getattr(backend, "simulation_only", False) and models is None:
                        return Action("done", {"summary": "Scripted fake teacher; no business work"})
                    if used >= limit:
                        # A budget, not a decision. Answering `done` here wrote the same trace a team
                        # that believed it had finished writes: the episode reason became `all_done`,
                        # so a rollout that ran out of calls at a high score was indistinguishable
                        # from one that stopped on purpose, and the near-miss rule that exists to
                        # give it one more rollout could never see it.
                        raise BudgetExhausted("teacher model call budget exhausted")
                    return await policy(obs)

                observed = _BackendEvidence(backend.backend(worker), worker, backend_failures)
                workers.append(WorkerAgent(worker, titles[worker], bounded, observed, budget))
            result = asyncio.run(
                Episode(
                    workers,
                    boss_id=boss,
                    brief=read(work / "tasks" / task_id / "assignment.json")["brief"],
                    runtime=runtime,
                    seconds=remaining() * 0.8,
                    direct_messages=False
                    if (work / "world/POPULATION.json").exists()
                    else config.get("teacher", {}).get("direct_messages", True),
                ).run()
            )
            if (work / "world/POPULATION.json").exists() and hasattr(backend, "export"):
                backend.export(task_id, deadline)
        except Exception as exc:  # noqa: BLE001 -- failures are the safeguard's output
            classification, reason = "environment_error", f"{type(exc).__name__}: {exc}"
        except BaseException as exc:  # noqa: BLE001 -- release resources, then propagate interruption
            interrupted = exc
        finally:
            try:
                backend.quiesce()
            except Exception as exc:  # noqa: BLE001
                classification, reason = "environment_error", f"quiesce: {type(exc).__name__}: {exc}"
        if interrupted is not None:
            try:
                backend.close()
            finally:
                raise interrupted
        try:
            env, defects, summary = _signals(runtime)
            episode_failed = result and result["reason"] not in ("all_done", "budget_exhausted")
            if backend_failures:
                classification, reason = "environment_error", backend_failures
            # Policy summaries, rendered text and tool output are untrusted:
            # workers can print an error or repeat an invalid action on purpose.
            # Keep these signals for diagnosis, never as grounds for a discard.
            # Grade completed, quiescent episodes even when teacher text reports a
            # defect; scores/contributions remain useful diagnostic evidence.
            if result and classification is None:
                try:
                    remaining()
                    can_judge = models is not None or not getattr(backend, "simulation_only", False)
                    if (work / "tasks" / task_id / "verifier.json").exists():
                        from .staged_grading import grade as grade_staged

                        grading_deadline = time.monotonic() + budgets.get("grading_seconds", seconds)

                        def grading_remaining():
                            left = grading_deadline - time.monotonic()
                            if left <= 0:
                                raise TimeoutError("Independent grading allowance exhausted")
                            return left

                        grading = _Models(
                            config, attempt / "grading-models", models, grading_remaining, grading_remaining
                        )

                        report = grade_staged(
                            root, work, task_id, backend.clients, models=grading if can_judge else None
                        )
                    else:
                        report = grader.grade(
                            work, task_id, backend.clients, models=grading if can_judge else None
                        )
                    if not isinstance(report, dict):
                        raise TypeError("grader report must be an object")
                    score = report["score"]
                    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                        raise ValueError("grader score must be finite and between zero and one")
                    classification = _grade_class(report, backend.clients)
                    if classification:
                        reason = report
                except Exception as exc:  # noqa: BLE001
                    classification, reason = "grader_error", f"{type(exc).__name__}: {exc}"
                    if not isinstance(report, dict):
                        report = None
                    if report is not None and not (
                        type(report.get("score")) in (int, float)
                        and math.isfinite(report["score"])
                        and 0 <= report["score"] <= 1
                    ):
                        report = {**report, "score": None}
            if classification is None and episode_failed and _provider_outage(result):
                # The model provider, not the task or the environment, ended the episode: nothing is
                # known about feasibility, so the step fails and a rerun tries the rollout again.
                classification, reason = "model_unavailable", _provider_outage(result)
            if classification is None:
                classification = (
                    "teacher_passed"
                    if not episode_failed and report["score"] >= threshold and report.get("passed", True)
                    else "teacher_failed"
                )
                reason = (
                    "harness did not finish cleanly" if episode_failed else "grade compared with threshold"
                )
                if env or defects:
                    reason = {"grade": reason, "unverified_environment": env, "unverified_defects": defects}
        except Exception as exc:  # noqa: BLE001
            classification, reason = "environment_error", f"evidence: {type(exc).__name__}: {exc}"
        finally:
            try:
                try:
                    if (work / "world/POPULATION.json").exists() and getattr(
                        backend, "world", None
                    ) is not None:
                        backend.world.reset()
                        baseline = initial_states(work)
                        if any(
                            client.current(backend.world.sid)["stored_state"] != baseline[app]
                            for app, client in backend.clients.items()
                        ):
                            raise ValueError("Trial reset did not restore the accepted baseline")
                        reset_proof = {"status": "pass", "at": now(), "apps": sorted(baseline)}
                finally:
                    backend.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"cleanup: {type(exc).__name__}: {exc}")
                classification, reason = "environment_error", errors
        diagnostic_path = attempt / "diagnostics.json"
        write(diagnostic_path, {"class": classification, "reason": reason, "cleanup_errors": errors})
        evidence = [
            str(diagnostic_path.relative_to(folder)),
            *[
                str(p.relative_to(folder))
                for p in sorted(attempt.rglob("*"))
                if p.is_file()
                and p.is_relative_to(runtime)
                and not ({"hub-cache", "node_modules", "browser.tar"} & set(p.parts))
            ],
        ]
        # What the rollout was given and what it spent, beside what it scored. A score alone cannot
        # tell "tried and failed" from "never really tried", and the controller reads the retry
        # decision from here rather than re-deriving it from the trajectory summary.
        budget_record = {
            "episode_reason": (result or {}).get("reason"),
            "worker_reasons": {
                worker: (row or {}).get("reason")
                for worker, row in ((result or {}).get("workers") or {}).items()
            },
            "seconds": seconds,
            "model_calls": limit,
            "model_calls_used": used,
            "grading_seconds": budgets.get("grading_seconds", seconds)
            if (work / "world/POPULATION.json").exists()
            else None,
        }
        output = {
            "kind": trial_kind,
            "reference_informed": trial_kind == "teacher",
            "unavailable_worker": unavailable_worker,
            "withheld_inputs": getattr(backend, "withheld_inputs", None),
            "reset": reset_proof,
            "task_id": task_id,
            "at": now(),
            "elapsed_seconds": time.monotonic() - started,
            "class": classification.replace("teacher_", trial_kind + "_"),
            "reason": reason,
            "score": report.get("score") if report else None,
            "threshold": threshold,
            "budget": budget_record,
            "retryable": retry_reason(classification, report.get("score") if report else None, budget_record),
            "evidence_refs": evidence,
            "contributions": report.get("contributions", {}) if report else {},
            "trajectory_summary": {"episode": result, "workers": summary},
            "trajectory_runtime": str(runtime.relative_to(folder)),
            "model": config.get("models", {}).get("worker_policy"),
            "model_calls_used": used,
            "simulation_only": getattr(backend, "simulation_only", False),
        }
        receipt_name = "TEACHER.json" if trial_kind == "teacher" else "TRIAL.json"
        write(attempt / receipt_name, output)
        status_path = task / "STATUS.json"
        status = read(status_path) if status_path.exists() else {}
        status.update(
            {
                f"{trial_kind}_class": output["class"],
                f"{trial_kind}_score": output["score"],
                f"{trial_kind}_report": str((directory / receipt_name).relative_to(folder)),
            }
        )
        if classification in DISCARD_CLASSES and not (folder / "world/POPULATION.json").exists():
            status.update(status="discarded", reason=reason)
        write(status_path, status)
        write(directory / receipt_name, output)
        return output
