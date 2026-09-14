"""Resume verification of a healthy completed desktop episode; preserve the original report."""

import time
from pathlib import Path

from company_envs.storage import digest, now, read, write

from .grader_author import _models
from .staged_grading import evaluate, proof_key
from .trial_diagnostics import collect_saved_trial


def regrade_trial(root, folder, task_id, *, kind, attempt):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    checkpoint = read(folder / "runtime/trials" / task_id / kind / f"{attempt:02d}" / "RESULT.json")
    original = checkpoint["result"]
    pending = {"verification_incomplete", "grader_error", "judge_unavailable"}
    if original["class"] not in pending | {kind + "_passed", kind + "_failed"}:
        raise ValueError("Regrading cannot promote an environment failure")
    if original.get("simulation_only") or original.get("reset", {}).get("status") != "pass":
        raise ValueError("Regrading requires a real episode with an exact successful reset")
    if original["budget"]["episode_reason"] not in {"all_done", "budget_exhausted"}:
        raise ValueError("The original worker episode did not complete normally")
    receipt, runtime, data = collect_saved_trial(folder, task_id, kind=kind, attempt=attempt)
    task = folder / "tasks" / task_id
    for name in ("assignment.json", "workflow.json", "assessment.json", "WORLD.json"):
        if read(task / name) != read(runtime.parent / "tasks" / task_id / name):
            raise ValueError(f"The accepted task/world changed after the episode: {name}")
    key = proof_key(root, folder, task_id)
    calibration = read(task / "verifier-calibration.json")
    if calibration["status"] != "accepted" or calibration["proof_key"] != key:
        raise ValueError("Regrading requires current accepted calibration")
    if original["class"] not in pending:
        previous_grade = (
            folder / original["grade_report"]
            if original.get("grade_report")
            else runtime / "grades" / task_id / "staged/report.json"
        )
        if read(previous_grade)["proof_key"] == key:
            raise ValueError("A completed judgment cannot be rerolled under unchanged verification")
    identity = digest({"proof": key, "data": data, "original": receipt})
    directory = runtime / "grades" / task_id / "regraded" / identity
    if (directory / "RESULT.json").exists():
        return read(directory / "RESULT.json")
    # Identical packets reuse original judges; encoded packets have new request hashes.
    models = _models(root, runtime.parent.parent / "grading-models", "task_judgment")
    started = time.monotonic()
    report = evaluate(root, folder, task_id, data, models=models)
    write(directory / "evidence.json", report.pop("evidence"))
    write(directory / "snapshot.json", data)
    write(directory / "report.json", report)
    if not report["complete"]:
        classification = "verification_incomplete"
    else:
        classification = kind + (
            "_passed" if report["passed"] and report["score"] >= receipt["threshold"] else "_failed"
        )
    updated = {
        **receipt,
        "at": now(),
        "class": classification,
        "score": report["score"],
        "grade_report": str((directory / "report.json").relative_to(folder)),
        "reason": "Verification resumed over preserved real episode evidence; original trial record retained.",
        "regrading": {
            "original_class": receipt["class"],
            "original_receipt_hash": digest(receipt),
            "original_checkpoint": f"runtime/trials/{task_id}/{kind}/{attempt:02d}/RESULT.json",
            "proof_key": key,
            "input_hash": identity,
            "seconds": time.monotonic() - started,
        },
        "evidence_refs": [
            *receipt["evidence_refs"],
            *[
                str((directory / name).relative_to(folder))
                for name in ("evidence.json", "snapshot.json", "report.json")
            ],
        ],
    }
    write(directory / "RESULT.json", updated)
    name = "TEACHER.json" if kind == "teacher" else "TRIAL.json"
    write(folder / "runtime" / kind / task_id / name, updated)
    status_path = task / "STATUS.json"
    status = read(status_path) if status_path.exists() else {}
    status.update({f"{kind}_class": classification, f"{kind}_score": report["score"]})
    write(status_path, status)
    return updated
