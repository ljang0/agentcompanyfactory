"""Run a fixed set of tasks and report teacher outcomes with their evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from company_envs.storage import digest, read, run_lock, write

from . import controller

FAMILY_RULE = "Keep a family_id in one evaluation split; consider holding out entire companies too."
SPLIT_METHOD = "SHA-256 of family_id, modulo 5: zero is test, otherwise train (about 80/20)."


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


def _inside(base, relative):
    path = base / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"path escapes benchmark evidence: {relative}")
    return path


def _select(root, companies, split):
    if split not in ("train", "test", "all", "disk"):
        raise ValueError("split must be train, test, all, or disk")
    if not companies.is_dir():
        raise ValueError(f"companies directory does not exist: {companies}")
    tasks, datasets, seen = [], {}, set()
    for folder in sorted(companies.iterdir()):
        if not folder.is_dir() or not (folder / "tasks").is_dir():
            continue
        manifest = read(folder / "MANIFEST.json")
        company = read(folder / "company.json")
        cid = _identifier(manifest["company_id"])
        if cid in seen:
            raise ValueError(f"duplicate company: {cid}")
        seen.add(cid)
        run = manifest.get("source_run")
        if split == "disk":
            entries = [{"workflow_id": p.parent.name} for p in sorted(folder.glob("tasks/*/workflow.json"))]
        else:
            _identifier(run)
            if run not in datasets:
                datasets[run] = read(root / "runs" / run / "dataset.json")
            dataset = datasets[run]
            if split != "all" and dataset.get("split_rule") != FAMILY_RULE:
                raise ValueError(f"unsupported split rule in run {run}")
            entries = [w for w in dataset["workflows"] if w["company_id"] == cid]
        ids = set()
        for entry in sorted(entries, key=lambda w: w["workflow_id"]):
            task_id = _identifier(entry["workflow_id"])
            if task_id in ids:
                raise ValueError(f"duplicate task: {cid}/{task_id}")
            ids.add(task_id)
            family = entry.get("family_id")
            if split in ("train", "test"):
                if not isinstance(family, str) or not family:
                    raise ValueError(f"missing family_id: {cid}/{task_id}")
                assigned = (
                    "test" if int(hashlib.sha256(family.encode()).hexdigest(), 16) % 5 == 0 else "train"
                )
                if assigned != split:
                    continue
            workflow = read(folder / "tasks" / task_id / "workflow.json")
            cell = workflow.get("feature_cell") or {}
            software = set(workflow.get("software_requirement_ids", []))
            apps = {
                a for s in company.get("software", []) if s["id"] in software for a in s["catalog_app_ids"]
            }
            apps.update(c.split(".")[0] for c in cell.get("collections", []))
            apps.update(a for c in workflow.get("contributions", []) for a in c.get("apps", []))
            tasks.append(
                {
                    "company_id": cid,
                    "task_id": task_id,
                    "source": str(folder.resolve()),
                    "source_run": run,
                    "family_id": family,
                    "sector": company.get("sector") or "unknown",
                    "decision_type": cell.get("decision_type") or workflow.get("decision_type") or "unknown",
                    "difficulty": workflow.get("difficulty"),
                    "apps": sorted(apps),
                }
            )
    return sorted(tasks, key=lambda t: (t["company_id"], t["task_id"]))


def _snapshot(source, target, task_id):
    if (target / "SNAPSHOT.json").exists():
        return
    # Only an incomplete copy is replaced; completed controller attempts survive.
    if target.exists():
        shutil.rmtree(target)
    for relative in ("MANIFEST.json", "company.json", "apps.json", "world", f"tasks/{task_id}"):
        path = source / relative
        if path.is_symlink() or any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError(f"input symlink: {path}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, destination)
        elif path.exists():
            shutil.copy2(path, destination)
    write(target / "SNAPSHOT.json", {"source": str(source), "task_id": task_id})


class _TeacherSteps(controller.DefaultSteps):
    """Use the teacher as the live policy, after preparing its calibrated grader."""

    def episode(self, ctx):
        if ctx.dry_run:
            return super().episode(ctx)
        from .hub_vm import stop_company

        stop_company(ctx.work)
        return {"skipped": True, "reason": "the teacher step runs the benchmark policy"}

    def grade(self, ctx):
        # DefaultSteps authors and calibrates before grading. A baseline score of
        # zero is expected, and must not prevent the teacher from attempting work.
        return {"ok": True, "baseline": super().grade(ctx)}


def _artifact(folder, state, step):
    relative = state.get("steps", {}).get(step, {}).get("artifacts", {}).get(f"{step}.json")
    if relative is None:
        return {}
    runtime = controller.task_runtime(folder, state["task_id"])  # artifact paths are relative to it
    path = _inside(runtime, relative)
    if not path.resolve().is_relative_to((runtime / "controller/artifacts").resolve()):
        raise ValueError("step receipt is outside controller artifacts")
    return read(path)


def _short(value):
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= 1000 else text[:997] + "..."


def _trajectory(folder, task_id, report, out):
    work = controller.task_runtime(folder, task_id) / "controller/company"
    runtime = (
        _inside(work, report["trajectory_runtime"]) if report.get("trajectory_runtime") else work / "runtime"
    )
    rows = []
    # A teacher runtime keeps traces with its VMs; the controller files a finished worker
    # episode's traces under runtime/episodes/<task>/<policy>/<worker>/trace.
    traces = [
        *runtime.glob("vms/*/trace/events.jsonl"),
        *runtime.glob(f"episodes/{task_id}/*/*/trace/events.jsonl"),
    ]
    for path in sorted(traces):
        screen, observation, actions = {}, {}, {}
        for line in path.read_text().splitlines():
            event = json.loads(line)
            kind = event["event"]
            if kind == "screen":
                screenshot = _inside(path.parent, event["file"])
                screen = {
                    "text": _short(event.get("text", "")),
                    "screenshot": str(screenshot.relative_to(out)) if screenshot.exists() else None,
                }
            elif kind == "observation":
                observation = {k: event[k] for k in ("brief", "remaining_actions") if k in event}
            elif kind == "action":
                row = {
                    "worker": path.parent.parent.name,
                    "worker_step": event["number"],
                    "elapsed": event["elapsed"],
                    "action": {"name": event["name"], "arguments": event["arguments"]},
                    "observation_summary": _short({**observation, "screen": screen.get("text")}),
                    "screenshot_path": screen.get("screenshot"),
                    "result_summary": None,
                    "app_state_diff_summary": None,
                }
                actions[event["number"]] = row
                rows.append(row)
            elif kind == "result" and event["number"] in actions:
                actions[event["number"]]["result_summary"] = _short(event["result"])
    rows.sort(key=lambda r: (r["elapsed"], r["worker"], r["worker_step"]))
    for index, row in enumerate(rows):
        row["step_index"] = index
    # The grader saves selected final evidence, not every app or every action.
    # Attach its available diffs once without claiming they belong to this action.
    evidence = runtime / "grades" / task_id / "evidence.json"
    if rows and evidence.exists():
        packet = read(evidence)
        diffs = [
            {"app": item["app_id"], "selector": item["selector"], "diff": _short(item["state_diff"])}
            for check in packet.get("checks", [])
            for item in check.get("evidence", [])
            if item.get("app_id") and item.get("state_diff") is not None
        ]
        if diffs:
            rows[-1]["app_state_diff_summary"] = {
                "scope": "selected final grader evidence; not attributed to this action",
                "diffs": diffs,
                "evidence_path": str(evidence.relative_to(out)),
            }
    return rows


def _rates(rows):
    attempted = [r for r in rows if not r["simulation_only"]]
    successes = sum(r["success"] for r in attempted)
    return {
        "tasks": len(rows),
        "attempted": len(attempted),
        "successes": successes,
        "success_rate": successes / len(attempted) if attempted else None,
    }


def _report(out, options, rows):
    grouped = {}
    for field in ("sector", "decision_type", "difficulty", "app"):
        groups = {}
        for row in rows:
            values = row["apps"] or ["unknown"] if field == "app" else [row[field]]
            for value in values:
                if value is not None:
                    groups.setdefault(str(value), []).append(row)
        grouped[field] = {name: _rates(group) for name, group in sorted(groups.items())}
    failures = dict(sorted(Counter(r["failure_class"] for r in rows if r["failure_class"]).items()))
    report = {
        "options": options,
        "overall": _rates(rows),
        "by": grouped,
        "failure_classes": failures,
        "results": rows,
    }
    write(out / "RESULTS.json", report)
    lines = [
        "# Benchmark results",
        "",
        f"Split: {options['split']}. Agent: {options['agent']}. Model: {options['model']}.",
        "",
        "Dry runs are simulations and do not enter success-rate denominators.",
        "Live failures, including setup failures, remain in the denominator. App groups can overlap.",
        "",
        "| Group | Tasks | Attempted | Successes | Success rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]

    def append(label, counts):
        rate = counts["success_rate"]
        rate = "N/A" if rate is None else f"{rate:.1%}"
        label = label.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {label} | {counts['tasks']} | {counts['attempted']} | {counts['successes']} | {rate} |"
        )

    append("Overall", report["overall"])
    for field, groups in grouped.items():
        for name, counts in groups.items():
            append(f"{field}: {name}", counts)
    lines += ["", "Failure classes: " + (", ".join(f"{k}={v}" for k, v in failures.items()) or "none"), ""]
    (out / "RESULTS.md").write_text("\n".join(lines))
    return report


def benchmark(
    root,
    companies_dir,
    split,
    agent,
    model,
    out_dir,
    *,
    dry_run=True,
    budgets=None,
    base_image=None,
    browser_dir=None,
    host_ip=None,
):
    """Run teacher attempts in task copies, resuming completed results unchanged.

    As in run-company, the default is an offline fake-backend dry run. Live runs
    require dry_run=False, VM inputs and a positive controller model-call budget.
    Train/test apply the dataset's family rule with a fixed roughly 80/20 hash.
    All selects exported dataset tasks; disk includes every task folder instead.
    """
    if agent != "teacher":
        raise ValueError("teacher is the only supported agent")
    if not isinstance(model, str) or "/" not in model or not all(model.split("/", 1)):
        raise ValueError("model must be a provider/model spec")
    if Path(out_dir).is_symlink():
        raise ValueError("benchmark output must not contain symlinks")
    root, companies, out = (Path(p).resolve() for p in (root, companies_dir, out_dir))
    if out.is_relative_to(companies) or companies.is_relative_to(out):
        raise ValueError("output and company inputs must be separate directories")
    if any(p.is_symlink() for p in out.rglob("*")):
        raise ValueError("benchmark output must not contain symlinks")
    budgets = controller._budgets(budgets if budgets is not None else controller.DEFAULT_BUDGETS)
    if not dry_run and (not all((base_image, browser_dir, host_ip)) or budgets["model_calls"] <= 0):
        raise ValueError("live benchmark requires VM inputs and a positive model_calls budget")
    tasks = _select(root, companies, split)
    options = {
        "root": str(root),
        "companies": str(companies),
        "split": split,
        "agent": agent,
        "model": model,
        "dry_run": dry_run,
        "budgets": budgets,
        "base_image": str(Path(base_image).resolve()) if base_image else None,
        "browser_dir": str(Path(browser_dir).resolve()) if browser_dir else None,
        "host_ip": host_ip,
        "split_method": SPLIT_METHOD if split in ("train", "test") else split,
    }
    manifest = {"version": 1, "options": options, "tasks": tasks}
    with run_lock(out):
        pin = out / "BENCHMARK.json"
        if pin.exists() and read(pin) != manifest:
            raise ValueError(
                "resume requires the same task selection, model and options; use another output directory"
            )
        write(pin, manifest)
        rows = []
        for task in tasks:
            directory = out / "tasks" / task["company_id"] / task["task_id"]
            result_path = directory / "RESULT.json"
            if result_path.exists():
                saved = read(result_path)
                if saved["options_hash"] != digest(options) or any(saved[k] != v for k, v in task.items()):
                    raise ValueError(f"result does not match this benchmark: {result_path}")
                if saved.get("cleanup_error"):
                    raise RuntimeError(f"resolve the recorded cleanup failure before resuming: {result_path}")
                rows.append(saved)
                continue
            folder = directory / "company"
            _snapshot(Path(task["source"]), folder, task["task_id"])
            steps, error, cleanup_error = _TeacherSteps(), None, None
            try:
                state = controller.run_company(
                    root,
                    folder,
                    task["task_id"],
                    budgets=budgets,
                    backend="fake" if dry_run else "mypcbench",
                    dry_run=dry_run,
                    steps=steps,
                    teacher_model=model,
                    base_image=base_image,
                    browser_dir=browser_dir,
                    host_ip=host_ip,
                    policies=["own"],  # the teacher is the benchmark policy; no extra sessions
                )
            except BaseException as exc:
                # The controller stops at a failed step. Release its owned VMs
                # and service before attempting another task or propagating Ctrl-C.
                checkpoint = controller.task_runtime(folder, task["task_id"]) / "CONTROLLER.json"
                if not checkpoint.exists():
                    raise
                state = read(checkpoint)
                if state["steps"]["serve"]["status"] != "pending":
                    ctx = controller.Context(
                        root, folder, state, "teardown", time.monotonic() + budgets["seconds"]["teardown"]
                    )
                    try:
                        with controller._deadline(budgets["seconds"]["teardown"]):
                            steps.teardown(ctx)
                    except Exception as cleanup:  # noqa: BLE001 -- preserve cleanup failure and stop the batch
                        cleanup_error = f"{type(cleanup).__name__}: {cleanup}"
                if not isinstance(exc, controller.ControllerError):
                    raise
                error = str(exc)
            teacher = _artifact(folder, state, "teacher")
            trajectory = _trajectory(folder, task["task_id"], teacher, out)
            trajectory_path = directory / "trajectory.jsonl"
            trajectory_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in trajectory))
            failure = teacher.get("class")
            if error or cleanup_error:
                failure = "controller_error"
            elif failure == "teacher_passed":
                failure = None
            elif not failure:
                failure = "not_evaluated" if dry_run else "missing_teacher_report"
            row = {
                **task,
                "simulation_only": dry_run or teacher.get("simulation_only", False),
                "success": failure is None
                and teacher.get("class") == "teacher_passed"
                and not (dry_run or teacher.get("simulation_only", False)),
                "failure_class": failure,
                "teacher_class": teacher.get("class"),
                "score": teacher.get("score"),
                "error": error,
                "cleanup_error": cleanup_error,
                "teacher_report": teacher,
                "trajectory": str(trajectory_path.relative_to(out)),
                "steps": len(trajectory),
                "controller": str(
                    (controller.task_runtime(folder, task["task_id"]) / "CONTROLLER.json").relative_to(out)
                ),
                "options_hash": digest(options),
            }
            write(result_path, row)
            rows.append(row)
            _report(out, options, rows)
            if cleanup_error:
                raise RuntimeError(f"benchmark stopped because cleanup failed: {cleanup_error}")
        return _report(out, options, rows)
