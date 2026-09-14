"""Declarative task rewards over hub snapshots; never execute model-authored code.

Author with ``author_grader(root, folder, task_id)``. Supply a reference to
``calibrate`` before ``grade``. Clients map app ids to HubClient instances;
the session id comes from runtime/sessions.json. Hub operations are reads except
``calibrate(golden=True)``, which seeds and replays an isolated golden session.

The pieces live in ``grader_core`` (schemas, selectors, predicate evaluation),
``grader_author`` (authoring), ``calibration`` (the proof) and ``judge`` (the judgment
model); this module grades an episode and re-exports their names so existing imports
keep working.
"""

import re
from pathlib import Path

from company_envs.storage import digest, now, read, write

from .calibration import (
    CalibrationError,
    _assert_initial,
    _inspect,
    _load,
    _proof_key,
    _state_results,
    calibrate,
)
from .grader_author import (
    AUTHOR_INSTRUCTIONS,
    MECHANICAL_FLOOR,
    _material_hashes,
    _models,
    _validate_grader,
    author_grader,
    authoring_payload,
    authoring_problems,
    bound_for_authoring,
    decisive_collections,
    floor_problem,
    grader_safe_feedback,
    mechanical_floor,
)
from .grader_core import (
    SKILL,
    VERSION,
    AppPath,
    Check,
    Contract,
    Predicate,
    TaskGrader,
    _assessment,
    _contributions,
    _criteria,
    _export_files,
    _file_hashes,
    _grading_context,
    _initial_files,
    _safe_path,
    _staged_files,
    _task,
    _tokens,
    _wiped_collections,
    assessment_score,
    evaluate_predicate,
    select,
)
from .judge import (
    JUDGE_VOTES,
    Judgment,
    TaskJudgment,
    _bounded,
    _compact,
    _evidence,
    _export_evidence,
    _file_text,
    _judge_base,
    _judge_one,
)

# Compatibility surface: every name other modules and tests import from here.
__all__ = [
    "AUTHOR_INSTRUCTIONS",
    "JUDGE_VOTES",
    "MECHANICAL_FLOOR",
    "SKILL",
    "VERSION",
    "AppPath",
    "CalibrationError",
    "Check",
    "Contract",
    "Judgment",
    "Predicate",
    "TaskGrader",
    "TaskJudgment",
    "_bounded",
    "_compact",
    "_export_evidence",
    "_file_text",
    "_grading_context",
    "_material_hashes",
    "_models",
    "_proof_key",
    "_safe_path",
    "_task",
    "_tokens",
    "author_grader",
    "authoring_payload",
    "authoring_problems",
    "bound_for_authoring",
    "calibrate",
    "evaluate_predicate",
    "floor_problem",
    "grade",
    "grader_safe_feedback",
    "mechanical_floor",
    "select",
]


def grade(folder, task_id, clients, *, models=None, label=None):
    """Grade a calibrated task. Omit models to leave semantic checks pending.

    Passing state checks get 60% of total weight when semantic checks exist;
    otherwise they get 100%. Semantic checks share 40%; pending earns zero.
    Each criterion has equal weight within its group, regardless of check count.
    With >=3 task workers, multiply by min(observed contributors / 3, 1).
    Reports/evidence are private under runtime/grades/<task_id>/. With ``label`` (the
    controller passes the worker policy's name) the exports are read from
    runtime/exports/<task_id>/<label>/ and the report lands in runtime/grades/<task_id>/<label>/;
    the calibration proof stays at the task level either way.
    """
    folder = Path(folder)
    if label is not None and not re.fullmatch(r"[a-zA-Z0-9_-]+", label):
        raise ValueError("grade label must be a plain name")
    draft, grader = _load(folder, task_id)
    sid, snapshots, errors = _inspect(folder, clients)
    # Recover authored baselines only for unreachable apps, never final values.
    initial = {
        app["app_id"]: snapshots[app["app_id"]]["initial_state"]
        if app["app_id"] in snapshots
        else read(_safe_path(folder, app["state_file"]))
        for app in read(folder / "apps.json")["apps"]
    }
    _assert_initial(draft, initial)
    _validate_grader(
        grader, _criteria(folder, task_id), initial, folder, decisive_collections(folder, task_id)
    )
    directory = folder / "runtime" / "grades" / task_id
    initial_files = _initial_files(folder, grader)
    try:
        proof = read(directory / "calibration.json")
    except (OSError, ValueError) as exc:
        raise CalibrationError("calibrate this grader before grading") from exc
    if (
        not proof.get("accepted")
        or proof.get("proof_key") != _proof_key(draft, initial)
        or proof.get("initial_score") != 0
        or proof.get("reference_score") != 1
        or proof.get("initial_file_hashes", {}) != _file_hashes(initial_files)
        # A proof written before the mechanical floor existed is stale in the way that matters:
        # it certifies a grader that credits any edit. Recomputed, never read from the report.
        or floor_problem(mechanical_floor(grader))
        or any(
            row["status"] == "pass" for row in _state_results(grader, initial, initial, exports=initial_files)
        )
    ):
        raise CalibrationError("missing, rejected or stale calibration; recalibrate")
    final = {app: row["current_state"] for app, row in snapshots.items()}
    wiped = _wiped_collections(initial, final)
    exports = _export_files(folder / "runtime/exports" / task_id / (label or ""))
    rows = _state_results(grader, initial, final, exports=exports)
    packets = [
        _evidence(
            folder,
            check,
            snapshots,
            draft["initial_material_hashes"],
            exports=exports,
            initial_files=_staged_files(folder),  # the whole staged desktop, not only predicate-named files
        )
        for check in grader.checks
        if check.kind != "state"
    ]
    packet = {
        "exported_files": _file_hashes(exports),
        "public_brief": read(_task(folder, task_id) / "assignment.json"),
        "success_criteria": _criteria(folder, task_id),
        "completion_outcomes": read(_task(folder, task_id) / "workflow.json")
        .get("completion", {})
        .get("outcomes", []),
        "checks": packets,
    }
    pending = []
    for item in packets:
        changed = any(ev["changed"] for ev in item["evidence"])
        status = "fail" if item["errors"] or not changed else "pending"
        rows.append(
            {
                "id": item["check"]["id"],
                "kind": item["check"]["kind"],
                "status": status,
                "reason": "missing evidence or no task-introduced change"
                if status == "fail"
                else "requires judgment",
            }
        )
        if status == "pending":
            pending.append(item)
    receipt = []
    judge_unavailable = False
    if models is not None and pending:
        # One judge call per check, carrying only that check's evidence: the judge reads the
        # content the rubric names instead of a trimmed dump of every collection, and one
        # oversized or failed check cannot take the others down with it. The private
        # completion outcomes stay out of the judge's view.
        base = _judge_base(folder, task_id, exports)
        by_id = {}
        for item in pending:
            check_id = item["check"]["id"]
            judgment, error, receipts = _judge_one(models, base, item)
            receipt.extend(r for r in receipts if r != "unavailable")
            if judgment is not None:
                by_id[check_id] = judgment
            else:
                errors[f"judgment:{check_id}"] = error
                if "unavailable" in receipts:
                    judge_unavailable = True
        for row in rows:
            if row["id"] in by_id:
                judgment = by_id[row["id"]]
                row.update(
                    status=judgment.status, reason=judgment.reason, evidence_refs=judgment.evidence_refs
                )
    by_id = {row["id"]: row for row in rows}
    criterion_scores = {}
    for check in grader.checks:
        criterion_scores.setdefault(check.criterion_ref, []).append(by_id[check.id]["status"] == "pass")
    state_refs = {c.criterion_ref for c in grader.checks if c.kind == "state"}
    semantic_refs = set(criterion_scores) - state_refs

    def average(refs):
        return sum(sum(criterion_scores[r]) / len(criterion_scores[r]) for r in refs) / len(refs)

    state_score = average(state_refs)
    check_score = 0.6 * state_score + 0.4 * average(semantic_refs) if semantic_refs else state_score
    contributions = _contributions(
        folder, task_id, grader, rows, snapshots, sid, exports=exports, initial_files=initial_files
    )
    factor = min(contributions["observed_workers"] / 3, 1) if contributions["required_workers"] else 1
    assessed = _assessment(folder, task_id)
    assessment_result = None
    if assessed is not None:
        assessment_result = assessment_score(
            assessed, [by_id[c.id] | {"criterion_ref": c.criterion_ref} for c in grader.checks]
        )
        check_score, factor = assessment_result["score"], 1
    report = {
        "task_id": task_id,
        "sid": sid,
        "at": now(),
        "score": check_score * factor,
        "check_score": check_score,
        "state_score": state_score,
        "passed": all(row["status"] == "pass" for row in rows) and contributions["status"] == "pass",
        "checks": [by_id[c.id] | {"criterion_ref": c.criterion_ref} for c in grader.checks],
        "contributions": contributions,
        "errors": errors,
        "evidence_hash": digest(packet),
        "judgment_receipt": receipt,
        "judge_unavailable": judge_unavailable,
        # Collections that lost most of their seeded records during the episode: an app that
        # reset itself to demo data, not work a team did. The teacher treats it as an
        # environment error; a worker episode is graded as it stands but flagged.
        "wiped_collections": wiped,
        "calibration_proof_key": proof["proof_key"],
    }
    if assessment_result is not None:
        report["assessment"] = assessment_result
        report["passed"] = (
            report["passed"] and assessment_result["must_pass_satisfied"] and assessment_result["complete"]
        )
        report["dependency_consumption"] = "unmeasured; requires independent event and outcome verification"
    output = directory / label if label else directory
    write(output / "evidence.json", packet)
    write(output / "report.json", report)
    return report
