"""Assessment-weighted staged grading over isolated Python, content and trusted writes."""

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from company_envs.storage import digest, now, read, write

from .evidence_encoding import encode_actors
from .grader_author import authoring_payload, full_initial_states
from .grader_core import _assessment, _export_files, _staged_files, _task, assessment_score
from .judge import JUDGMENT_INPUT_LIMIT, _file_text, _judge_one
from .python_verifier import VerifierUnavailable, run_verifier
from .task_assessment import assessment_sources, require_task_world
from .trial_evidence import read_events, record_changes


def proof_key(root, folder, task_id):
    task = _task(Path(folder), task_id)
    declared = read(task / "verifier.json")
    if declared["code_hash"] != digest((task / "verifier.py").read_bytes()):
        raise VerifierUnavailable("Verifier source differs from its author receipt")
    if declared["input_hash"] != digest(authoring_payload(root, folder, task_id)):
        raise VerifierUnavailable("Verifier input requirements changed")
    modules = (
        "staged_grading.py",
        "python_verifier.py",
        "verifier_runner.py",
        "judge.py",
        "grader_core.py",
        "task_assessment.py",
        "trial_evidence.py",
        "evidence_encoding.py",
    )
    return digest(
        {
            "calibration_protocol": 3,
            "verifier": declared["code_hash"],
            "input": declared["input_hash"],
            "implementation": {
                name: digest(ast.dump(ast.parse(Path(__file__).with_name(name).read_text())))
                for name in modules
            },
        }
    )


def evidence_items(data, sources, *, force_encoding=False):
    changes = record_changes(data["initial"], data["final"])
    items = [{"id": "changes", "changed": bool(changes), "current": {"changed_records": changes}}]
    items += [
        {"id": f"source:{i}", "reference": ref, "changed": False, "current": value}
        for i, (ref, value) in enumerate(sorted(sources.items()))
    ]
    items += [
        {"id": f"file:{i}", "path": path, "changed": True, "current": content}
        for i, (path, content) in enumerate(sorted(data.get("files", {}).items()))
    ]
    items.append({"id": "actors", "changed": bool(data.get("events")), "current": data.get("events", [])})
    if force_encoding or len(json.dumps(items)) > JUDGMENT_INPUT_LIMIT - 50_000:
        items[-1]["current"] = encode_actors(data.get("events", []), changes)
    return items


def evaluate(
    root, folder, task_id, data, *, models=None, votes=3, judge_unchanged=False, force_encoding=False
):
    """Return observed results, without requiring an already accepted calibration."""
    folder = Path(folder)
    require_task_world(root, folder, task_id)
    key = proof_key(root, folder, task_id)
    task = _task(folder, task_id)
    workflow = read(task / "workflow.json")
    assessment = _assessment(folder, task_id)
    if data["initial"] != full_initial_states(folder):
        raise VerifierUnavailable("Evaluation baseline differs from the accepted world")
    if not isinstance(data.get("final"), dict) or set(data["final"]) != set(data["initial"]):
        raise VerifierUnavailable("Final evidence must contain every app snapshot")
    if any(not isinstance(state, dict) for state in data["final"].values()):
        raise VerifierUnavailable("Final app snapshots must be native state objects")
    if any(set(data["initial"][app]) - set(state) for app, state in data["final"].items()):
        raise VerifierUnavailable("Final evidence is missing native state collections")
    expected = [i + 1 for i, c in enumerate(workflow["success_criteria"]) if c["method"] == "state"]
    mechanical = run_verifier(task / "verifier.py", data, expected)
    sources = assessment_sources(assessment, data["initial"])
    evidence = evidence_items(data, sources, force_encoding=force_encoding)
    base = {
        "public_brief": read(task / "assignment.json"),
        "success_criteria": workflow["success_criteria"],
        "assessment": assessment,
    }
    changed = bool(record_changes(data["initial"], data["final"]) or data.get("files"))
    rows, raw_judgments, receipts, errors = [], [], [], []

    def judge(check_id, rubric):
        if models is None:
            return "pending", "Semantic verification was not run", []
        item = {
            "check": {"id": check_id, "kind": "judgment", "rubric": rubric},
            "evidence": evidence,
            "errors": [],
        }
        if len(json.dumps({**base, "checks": [item]})) > JUDGMENT_INPUT_LIMIT:
            raise VerifierUnavailable(
                "Complete staged evidence exceeds the judge input limit after lossless encoding"
            )
        judgment, error, recs = _judge_one(models, base, item, votes=votes)
        receipts.extend(recs)
        if judgment is None:
            errors.append({"check_id": check_id, "error": error})
            return "error", str(error), []
        return judgment.status, judgment.reason, judgment.evidence_refs

    dependency_traces = []
    events = data.get("events", [])
    for dependency in assessment["dependencies"]:
        worker = dependency["worker_id"]
        produced = [e for e in events if e.get("worker_id") == worker and e.get("changes")]
        consumed = {
            consumer: [
                e
                for e in events
                if e.get("worker_id") == consumer
                and e.get("changes")
                and any(e["sequence"] > p["sequence"] for p in produced)
            ]
            for consumer in dependency["consumed_by"]
        }
        dependency_traces.append((dependency, bool(produced) and all(consumed.values())))
    requests = {}
    if changed or judge_unchanged:
        for criterion in assessment["criteria"]:
            number = criterion["criterion"]
            requests[f"criterion-{number}"] = (
                criterion["rubric"] + "\n" + str(workflow["success_criteria"][number - 1])
            )
    for dependency, traced in dependency_traces:
        if traced:
            requests[f"dependency-{dependency['worker_id']}"] = (
                "Verify this required worker contribution and its actual use by each named consumer: "
                + str(dependency)
                + "\nUse trusted actor records and surviving business content. The producer's relevant work must survive or be incorporated in the final result. A ceremonial acknowledgement, unrelated edit, a forwarded solved answer or a self-reported claim does not satisfy this dependency. Check the cited input and authority records. Do not infer actual consumption merely from message delivery or worker count. This check measures the trace, not counterfactual necessity; later ablations measure that."
            )
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {check_id: pool.submit(judge, check_id, rubric) for check_id, rubric in requests.items()}
        judgments = {check_id: future.result() for check_id, future in futures.items()}

    for criterion in assessment["criteria"]:
        number = criterion["criterion"]
        status, reason, refs = (
            judgments[f"criterion-{number}"]
            if changed or judge_unchanged
            else ("fail", "No task work changed the initial state or files", [])
        )
        raw_judgments.append({"criterion": number, "status": status, "reason": reason})
        check = mechanical["checks"].get(str(number))
        if check is not None and not check["passed"] and status not in {"pending", "error"}:
            status, reason = "fail", f"Mechanical condition failed: {check['reason']}; semantic: {reason}"
        rows.append(
            {
                "id": f"criterion-{number}",
                "criterion_ref": f"/success_criteria/{number - 1}",
                "status": status,
                "reason": reason,
                "evidence_refs": refs,
            }
        )
    dependencies = []
    for dependency, traced in dependency_traces:
        worker = dependency["worker_id"]
        if not traced:
            status, reason, refs = (
                "fail",
                "No recorded producer work followed by work from every required consumer",
                [],
            )
        else:
            status, reason, refs = judgments[f"dependency-{worker}"]
        dependencies.append({"worker_id": worker, "status": status, "reason": reason, "evidence_refs": refs})
    scored = assessment_score(assessment, rows)
    complete = (
        scored["complete"] and all(d["status"] in {"pass", "fail"} for d in dependencies) and not errors
    )
    passed = scored["must_pass_satisfied"] and complete and all(d["status"] == "pass" for d in dependencies)
    return {
        "at": now(),
        "task_id": task_id,
        "score": scored["score"],
        "passed": passed,
        "complete": complete,
        "assessment": scored,
        "checks": rows,
        "semantic_checks": raw_judgments,
        "dependencies": dependencies,
        "mechanical": mechanical,
        "errors": {e["check_id"]: e["error"] for e in errors},
        "judgment_receipts": receipts,
        "proof_key": key,
        "evidence_hash": digest({"base": base, "evidence": evidence}),
        "actor_evidence_encoding": evidence[-1]["current"].get("encoding")
        if isinstance(evidence[-1]["current"], dict)
        else "expanded",
        "necessity": "requires separate worker/input ablation trials",
        "evidence": {"base": base, "items": evidence},
    }


def grade(root, folder, task_id, clients, *, models=None, label=None):
    folder = Path(folder)
    task = _task(folder, task_id)
    calibration = read(task / "verifier-calibration.json")
    if calibration.get("status") != "accepted" or calibration.get("proof_key") != proof_key(
        root, folder, task_id
    ):
        raise VerifierUnavailable("Missing or stale staged verifier calibration")
    sid = read(folder / "runtime/sessions.json")["sid"]
    snapshots = {app: client.inspect(sid) for app, client in clients.items()}
    baseline = _staged_files(folder)
    exports = _export_files(folder / "runtime/exports" / task_id / (label or ""))
    files = {
        name: _file_text(path)
        for name, path in exports.items()
        if name != "EXPORT.json"
        and not (len(Path(name).parts) == 2 and Path(name).name == "manifest.json")
        and (name not in baseline or digest(path.read_bytes()) != digest(baseline[name].read_bytes()))
    }
    data = {
        "initial": {a: s["initial_state"] for a, s in snapshots.items()},
        "final": {a: s["current_state"] for a, s in snapshots.items()},
        "files": files,
        "events": read_events(folder, sid),
    }
    report = evaluate(root, folder, task_id, data, models=models)
    target = folder / "runtime/grades" / task_id / (label or "staged")
    write(target / "snapshot.json", data)
    write(target / "evidence.json", report.pop("evidence"))
    write(target / "report.json", report)
    return report
