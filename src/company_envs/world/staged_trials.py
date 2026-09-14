"""Explicit resumable desktop trials for frozen tasks with accepted calibration."""

import ast
import os
from pathlib import Path

from company_envs.config import load_config
from company_envs.storage import digest, now, read, write

from .staged_grading import proof_key
from .task_assessment import require_task_world
from .teacher import _LiveBackend, teacher_rollout


def scored_episodes(folder, directory, kind):
    """Count completed judgments once per episode, including a repaired verification."""
    completed = {kind + "_passed", kind + "_failed"}
    count = 0
    for path in directory.glob("*/RESULT.json"):
        result = read(path)["result"]
        judged = result.get("class") in completed
        runtime = result.get("trajectory_runtime")
        if runtime and not judged:
            judged = any(
                read(regrade).get("class") in completed
                for regrade in (folder / runtime / "grades").glob("*/regraded/*/RESULT.json")
            )
        count += judged
    return count


def run_trial(root, folder, task_id, *, kind, work, attempt=1, unavailable_worker=None, input_worker=None):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    require_task_world(root, folder, task_id)
    task = folder / "tasks" / task_id
    calibration = read(task / "verifier-calibration.json")
    if calibration.get("status") != "accepted" or calibration.get("proof_key") != proof_key(
        root, folder, task_id
    ):
        raise ValueError("Real trials require current accepted verifier calibration")
    if calibration.get("golden_hash") != digest(read(task / "golden.json")):
        raise ValueError("The current teacher reference has not passed this calibration")
    config = load_config(root)
    withheld = None
    if kind == "input-ablation":
        dependencies = read(task / "assessment.json")["dependencies"]
        dependency = next((d for d in dependencies if d["worker_id"] == input_worker), None)
        if dependency is None:
            raise ValueError("Input ablation requires an assessment worker via --input-worker")
        withheld = {"worker_id": input_worker, "references": dependency["inputs"]}
    elif input_worker is not None:
        raise ValueError("An input worker is only valid in an input-ablation trial")
    policy = config.get("worker_policy", {})
    budgets = {
        "seconds": policy.get("seconds", 1500),
        "actions": policy.get("actions", 150),
        "model_calls": config["design"]["vm_model_calls"],
        "threshold": 1.0,
    }
    if kind != "teacher":
        teacher = read(folder / "runtime/teacher" / task_id / "TEACHER.json")
        if (
            teacher["class"] != "teacher_passed"
            or teacher.get("simulation_only")
            or teacher.get("reset", {}).get("status") != "pass"
        ):
            raise ValueError("Ordinary and ablation trials require a successful real teacher and reset")
        grade_path = teacher.get("grade_report") or str(
            Path(teacher["trajectory_runtime"]) / "grades" / task_id / "staged/report.json"
        )
        grade = read(folder / grade_path)
        if grade.get("proof_key") != calibration["proof_key"] or not (
            grade.get("passed") and grade.get("complete")
        ):
            raise ValueError("The teacher requires complete grading under the current calibrated verifier")
    key = digest(
        {
            "proof": calibration["proof_key"],
            "calibration": digest(calibration),
            "golden": digest(read(task / "golden.json")) if kind == "teacher" else None,
            "kind": kind,
            "unavailable_worker": unavailable_worker,
            "withheld_inputs": withheld,
            "config": config,
            "budgets": budgets,
            "implementation": {
                name: digest(ast.dump(ast.parse((Path(__file__).parent / name).read_text())))
                for name in ("staged_trials.py", "teacher.py", "trial_access.py", "backends/mypcbench.py")
            },
        }
    )
    if attempt < 1:
        raise ValueError("Trial attempt must be positive")
    directory = folder / "runtime/trials" / task_id / kind / f"{attempt:02d}"
    if (directory / "RESULT.json").exists():
        saved = read(directory / "RESULT.json")
        if saved["input_hash"] != key:
            raise ValueError("Trial inputs changed; retained evidence needs explicit diagnosis")
        return saved
    if scored_episodes(folder, directory.parent, kind) >= 3:
        raise ValueError("Three scored trials are retained; diagnose the task before spending further")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise PermissionError("Real desktop trials require host read/write access to /dev/kvm")
    write(directory / "INPUT.json", {"at": now(), "input_hash": key, "kind": kind, "budgets": budgets})
    design = config["design"]
    inputs = {
        "base_image": Path(design["vm_base_image"]).expanduser(),
        "browser_dir": Path(design["vm_browser_dir"]).expanduser(),
        "host_ip": design["vm_host_ip"],
    }
    backend = _LiveBackend(root, folder, inputs, work_cache=Path(work).resolve(), withheld_inputs=withheld)
    result = teacher_rollout(
        root,
        folder,
        task_id,
        backend=backend,
        budgets=budgets,
        trial_kind=kind,
        unavailable_worker=unavailable_worker,
    )
    record = {"at": now(), "input_hash": key, "result": result}
    write(directory / "RESULT.json", record)
    return record
