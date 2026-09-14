"""Judge retained real trial outputs without certifying an interrupted environment."""

import json
from pathlib import Path

from company_envs.storage import digest, now, read, write

from .grader_author import _models
from .grader_core import _export_files, _staged_files
from .judge import _file_text
from .staged_grading import evaluate, proof_key


def collect_saved_trial(folder, task_id, *, kind, attempt):
    folder = Path(folder).resolve()
    checkpoint = read(folder / "runtime/trials" / task_id / kind / f"{attempt:02d}" / "RESULT.json")
    receipt = checkpoint["result"]
    runtime = (folder / receipt["trajectory_runtime"]).resolve()
    if not runtime.is_relative_to(folder):
        raise ValueError("Trial runtime must belong to this company")
    work = runtime.parent
    captured = read(runtime / "NATIVE-FINAL.json")
    if captured["status"] != "captured" or captured.get("errors"):
        raise ValueError("Diagnostic grading requires a complete actual native snapshot")
    exports = runtime / "exports" / task_id
    manifests = read(exports / "EXPORT.json")
    if set(manifests) != set(receipt["trajectory_summary"]["episode"]["workers"]):
        raise ValueError("Diagnostic grading requires every actual worker's export")
    for worker, manifest in manifests.items():
        if manifest["status"] != "exported":
            raise ValueError(f"Incomplete export: {worker}")
        for name, details in manifest["files"].items():
            path = (exports / worker / name).resolve()
            if not path.is_relative_to(exports) or digest(path.read_bytes()) != details["sha256"]:
                raise ValueError(f"Export changed: {worker}/{name}")
    histories = list((runtime / "evidence/history").glob("*.jsonl"))
    if len(histories) != 1:
        raise ValueError("Need one retained real episode's trusted write history")
    events = [json.loads(line) for line in histories[0].read_text().splitlines()]
    if (
        not events
        or len({e["sid"] for e in events}) != 1
        or [e["sequence"] for e in events] != list(range(1, len(events) + 1))
    ):
        raise ValueError("Incomplete or mixed actor history")
    baseline = _staged_files(work)
    files = {
        name: _file_text(path)
        for name, path in _export_files(exports).items()
        if name != "EXPORT.json"
        and not (len(Path(name).parts) == 2 and Path(name).name == "manifest.json")
        and (name not in baseline or digest(path.read_bytes()) != digest(baseline[name].read_bytes()))
    }
    data = {
        "initial": {app: row["initial_state"] for app, row in captured["apps"].items()},
        "final": {app: row["current_state"] for app, row in captured["apps"].items()},
        "files": files,
        "events": events,
    }
    return receipt, runtime, data


def diagnose_trial(root, folder, task_id, *, kind, attempt):
    root, folder = Path(root).resolve(), Path(folder).resolve()
    receipt, runtime, data = collect_saved_trial(folder, task_id, kind=kind, attempt=attempt)
    work = runtime.parent
    key = digest({"proof": proof_key(root, work, task_id), "data": data, "receipt": receipt})
    directory = runtime / "diagnostic-grades" / key
    if (directory / "RESULT.json").exists():
        return read(directory / "RESULT.json")
    models = _models(root, directory / "models", "task_judgment")
    report = evaluate(root, work, task_id, data, models=models)
    write(directory / "evidence.json", report.pop("evidence"))
    write(directory / "report.json", report)
    result = {
        "at": now(),
        "status": "diagnostic_only",
        "original_class": receipt["class"],
        "score": report["score"],
        "business_passed": report["passed"],
        "complete": report["complete"],
        "input_hash": key,
        "report": str((directory / "report.json").relative_to(folder)),
        "scope": "Actual retained native state, exported files and actor evidence. This does not repair the interrupted environment or change the original trial's classification or acceptance.",
    }
    write(directory / "RESULT.json", result)
    return result
