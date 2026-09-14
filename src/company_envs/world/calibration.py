"""Calibration: the zero-initial/one-reference proof a grader must hold before it grades.

The pinned draft is loaded and checked against the task, the untouched initial world must
fail every state check and the reference (a supplied final state or diff, or a replayed
golden) must pass them all; judged checks are put to the judge on both, and the judge
benchmark rows are written beside the calibration report.

Those two points do not separate work from vandalism: a check that asks only whether a field
moved fails on the untouched world and passes on the reference, and passes on gibberish too.
So the proof also requires the mechanical floor: at least half the mechanical score, weighted
as ``grade`` weights it, must rest on checks that compare a value.
"""

from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

from company_envs.storage import digest, now, read, write

from .grader_author import _validate_grader, decisive_collections, floor_problem, mechanical_floor
from .grader_core import (
    VERSION,
    TaskGrader,
    _contributions,
    _criteria,
    _export_files,
    _file_hashes,
    _grading_context,
    _initial_files,
    _safe_path,
    _task,
    evaluate_predicate,
)
from .judge import _evidence, _judge_base, _judge_one


class CalibrationError(ValueError):
    """The grader has no valid zero-initial/one-reference proof."""


class CalibrationUnavailable(CalibrationError):
    """The apps could not be reached, so nothing about this task was measured.

    Not a verdict on the grader, the golden or the world: the same class of thing as a model
    provider that is not answering. One company spent the last of its three repair rounds on
    ``golden replay: <urlopen error [Errno 111] Connection refused>`` -- the hub was not serving --
    and re-authored a golden that was never the problem. A caller with a repair budget must let this
    through without spending a round and without asking any author to rewrite anything.

    It subclasses CalibrationError so every existing handler keeps working unchanged.
    """


def _load(folder, task_id):
    draft = read(_task(folder, task_id) / "grader.json")
    if draft["version"] != VERSION:
        raise CalibrationError("unsupported grader version")
    grader = TaskGrader.model_validate(draft["grader"])
    criteria = _criteria(folder, task_id)
    brief = read(_task(folder, task_id) / "assignment.json")
    if (
        digest(criteria) != draft["criteria_hash"]
        or digest(brief) != draft["brief_hash"]
        or digest(_grading_context(folder, task_id)) != draft["grading_context_hash"]
    ):
        raise CalibrationError("task requirements changed; reauthor and recalibrate")
    return draft, grader


def _unreachable(exc):
    """Whether a failure is the apps not answering rather than an answer about this task.

    An ``HTTPError`` means the hub answered, so it is about the request; every other ``OSError``
    (``URLError``, a refused or reset connection, a timeout) means nothing was measured.
    """
    return isinstance(exc, OSError) and not isinstance(exc, HTTPError)


def _inspect(folder, clients, unreachable=None):
    """Snapshot every app's baseline. ``unreachable``, when given, collects the apps that did not
    answer at all, which is a different thing from an app whose answer broke the contract."""
    sid = read(Path(folder) / "runtime" / "sessions.json")["sid"]
    app_ids = [app["app_id"] for app in read(Path(folder) / "apps.json")["apps"]]
    snapshots, errors = {}, {}
    for app in app_ids:
        try:
            seen = clients[app].inspect(sid)
            if not all(isinstance(seen[k], dict) for k in ("initial_state", "current_state")):
                raise ValueError("initial/current state must be JSON objects")
            if "state_diff" not in seen:
                raise ValueError("missing state_diff")
            snapshots[app] = deepcopy(seen)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors[app] = f"{type(exc).__name__}: {exc}"
            if unreachable is not None and _unreachable(exc):
                unreachable.add(app)
    return sid, snapshots, errors


def _state_results(grader, initial, final, *, exports=None):
    rows = []
    for check in grader.checks:
        if check.kind != "state":
            continue
        try:
            passed = all(
                evaluate_predicate(p, initial, final, exports=exports)
                for p in [check.predicate, *check.guards]
            )
            reason = "predicates satisfied" if passed else "predicates not satisfied"
        except (ValueError, KeyError, TypeError) as exc:
            passed, reason = False, f"{type(exc).__name__}: {exc}"
        rows.append(
            {"id": check.id, "kind": check.kind, "status": "pass" if passed else "fail", "reason": reason}
        )
    return rows


def _proof_key(draft, initial):
    return digest({"version": VERSION, "draft": draft, "initial": initial})


def _assert_initial(draft, initial):
    if digest(initial) != draft["initial_hash"]:
        raise CalibrationError("inspected baseline differs from authored initial world")


def calibrate(
    folder,
    task_id,
    clients,
    *,
    reference_states=None,
    reference_diff=None,
    reference_exports=None,
    golden=False,
    models=None,
):
    """Accept only if every initial state check fails, every reference check passes, and the
    grader clears the mechanical floor.

    With a judge (models), every judged check is calibrated as well: it must pass on the
    reference state and show no change on the untouched initial state, so a rubric the
    judge cannot satisfy with the golden is rejected before any VM time. The report's
    scope says which happened; the agreement requires all_checks.

    reference_states is a complete {app_id: state} map. reference_diff replaces
    named top-level keys on the initial states (not the hub's deep-diff format).
    If neither is supplied, tasks/<id>/reference.json must contain exactly one
    of final_states or diff. Never infer a reference from clients' current states.
    Calibration covers state checks only; semantic evidence still needs grading.
    With golden=True, replay tasks/<id>/golden.json in a fresh session from the
    on-disk initial world. Require three contributing workers for rosters of
    three or more. Golden cannot be combined with another app-state reference.
    reference_exports names a separate directory containing worker/Desktop,
    Documents and Downloads files produced by a reference run. It can accompany
    either app reference, or golden app replay. reference.json may also name an
    exports directory relative to the company folder. Live exports are never
    inferred as reference evidence. Initial files come from staged VM payloads,
    or world materials before launch.
    """
    folder = Path(folder)
    draft, grader = _load(folder, task_id)
    if golden:
        from .golden import initial_states, replay_golden

        if reference_states is not None or reference_diff is not None:
            raise CalibrationError("golden cannot be combined with reference_states or reference_diff")
        initial = initial_states(folder)
    else:
        unreachable = set()
        _, snapshots, errors = _inspect(folder, clients, unreachable)
        if unreachable:
            missing = {app: errors[app] for app in sorted(unreachable)}
            raise CalibrationUnavailable(f"apps are not answering: {missing}")
        if errors:
            raise CalibrationError(f"cannot inspect calibration baseline: {errors}")
        initial = {app: row["initial_state"] for app, row in snapshots.items()}
    _assert_initial(draft, initial)
    _validate_grader(
        grader, _criteria(folder, task_id), initial, folder, decisive_collections(folder, task_id)
    )
    initial_files = _initial_files(folder, grader)
    noop = _state_results(grader, initial, initial, exports=initial_files)
    golden_sid = None
    if golden:
        golden_sid = f"golden-{uuid4().hex}"
        try:
            reference_states = replay_golden(folder, task_id, clients, golden_sid)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            kind = CalibrationUnavailable if _unreachable(exc) else CalibrationError
            raise kind(f"golden replay: {exc}") from exc
    if reference_states is None and reference_diff is None:
        reference = read(_task(folder, task_id) / "reference.json")
        if set(reference) - {"exports"} not in ({"final_states"}, {"diff"}):
            raise CalibrationError("reference.json requires final_states or diff, with optional exports")
        if "exports" in reference:
            if reference_exports is not None:
                raise CalibrationError("supply reference exports only once")
            reference_exports = _safe_path(folder, reference["exports"])
        reference_states, reference_diff = reference.get("final_states"), reference.get("diff")
    if (reference_states is None) == (reference_diff is None):
        raise CalibrationError("supply exactly one reference_states or reference_diff")
    if reference_diff is not None:
        reference_states = deepcopy(initial)
        for app, changes in reference_diff.items():
            if app not in initial or not isinstance(changes, dict) or set(changes) - initial[app].keys():
                raise CalibrationError("reference diff must replace known app top-level keys")
            reference_states[app].update(deepcopy(changes))
    if set(reference_states) != set(initial) or any(
        not isinstance(state, dict) or set(state) != set(initial[app])
        for app, state in reference_states.items()
    ):
        raise CalibrationError("reference states must preserve the app/collection contract")
    if reference_exports is not None:
        reference_exports = Path(reference_exports)
        live_exports = folder / "runtime/exports"
        if reference_exports.resolve().is_relative_to(live_exports.resolve()):
            raise CalibrationError("reference exports must be separate from live episode exports")
        if not reference_exports.is_dir():
            raise CalibrationError("reference exports directory is missing")
    reference_files = _export_files(reference_exports)
    reference_rows = _state_results(grader, initial, reference_states, exports=reference_files)
    failures = [f"{row['id']}: initial must fail" for row in noop if row["status"] != "fail"]
    failures.extend(f"{row['id']}: reference must pass" for row in reference_rows if row["status"] != "pass")
    # The zero/one pair is satisfied trivially by a check that only asks whether a field moved:
    # the untouched world fails it and the reference passes it, and so does vandalism. The floor
    # is the third thing the proof has to say, and it is recomputed here rather than read from the
    # draft, so an edited grader.json cannot carry a stale measurement past this point.
    floor = mechanical_floor(grader)
    below_floor = floor_problem(floor)
    if below_floor:
        failures.append(below_floor)
    contributions = None
    if golden:
        snapshots = {
            app: {"initial_state": initial[app], "current_state": reference_states[app]} for app in initial
        }
        contributions = _contributions(
            folder,
            task_id,
            grader,
            reference_rows,
            snapshots,
            golden_sid,
            attribution_dir=folder / "runtime" / "golden" / task_id / "attribution",
            exports=reference_files,
            initial_files=initial_files,
        )
        if contributions["status"] != "pass":
            failures.append(
                "contributions: "
                + (
                    "; ".join(contributions["problems"])
                    if contributions.get("problems")
                    else f"golden requires {contributions['required_workers']} distinct workers; observed {contributions['observed_workers']}"
                )
            )
    judged = [c for c in grader.checks if c.kind != "state"]
    judged_rows, bench, unavailable = [], [], []
    if judged and models is not None:
        base = _judge_base(folder, task_id, reference_files)
        golden_snaps = {
            app: {"initial_state": initial[app], "current_state": reference_states[app], "state_diff": {}}
            for app in initial
        }
        noop_snaps = {
            app: {"initial_state": initial[app], "current_state": initial[app], "state_diff": {}}
            for app in initial
        }
        for check in judged:
            row = {"id": check.id, "kind": check.kind}
            noop_item = _evidence(
                folder,
                check,
                noop_snaps,
                draft["initial_material_hashes"],
                exports=initial_files,
                initial_files=initial_files,
            )
            if any(ev["changed"] for ev in noop_item["evidence"]):
                failures.append(f"{check.id}: untouched initial must show no task-introduced change")
            item = _evidence(
                folder,
                check,
                golden_snaps,
                draft["initial_material_hashes"],
                exports=reference_files,
                initial_files=initial_files,
            )
            if item["errors"]:
                row.update(
                    reference="fail", reason="evidence selection failed: " + "; ".join(item["errors"])[:400]
                )
                failures.append(f"{check.id}: {row['reason']}")
            elif not any(ev["changed"] for ev in item["evidence"]):
                row.update(reference="fail", reason="golden introduces no change in this check's evidence")
                failures.append(f"{check.id}: {row['reason']}")
            else:
                judgment, error, _receipts = _judge_one(models, base, item)
                status = judgment.status if judgment else "error"
                row.update(reference=status, reason=(judgment.reason if judgment else error))
                if status != "pass":
                    failures.append(f"{check.id}: judge must pass the reference ({status}: {row['reason']})")
                # Judge benchmark: the unchanged packet is the negative example (one vote is enough
                # to learn whether the judge passes evidence with no task-introduced change).
                negative, _err, _r = _judge_one(models, base, noop_item, votes=1)
                if negative is not None and negative.status == "pass":
                    failures.append(f"{check.id}: judge falsely passed the untouched initial outcome")
                elif negative is None or negative.status != "fail":
                    unavailable.append(f"{check.id}: negative semantic calibration is inconclusive")
                bench.append(
                    {
                        "check_id": check.id,
                        "kind": check.kind,
                        "golden_verdict": status,
                        "golden_reason": row["reason"],
                        "initial_verdict": negative.status if negative else "error",
                    }
                )
            judged_rows.append(row)
        write(
            folder / "runtime" / "grades" / task_id / "judge_bench.json",
            {"at": now(), "task_id": task_id, "rows": bench},
        )
    accepted = not failures and not unavailable
    report = {
        "accepted": accepted,
        "scope": "all_checks" if (models is not None or not judged) else "state_checks_only",
        "judged_checks": judged_rows,
        "failures": failures,
        "unavailable": unavailable,
        "proof_key": _proof_key(draft, initial),
        "reference_hash": digest(reference_states),
        "initial_file_hashes": _file_hashes(initial_files),
        "reference_file_hashes": _file_hashes(reference_files),
        "mechanical_floor": floor,
        "initial_score": sum(row["status"] == "pass" for row in noop) / len(noop),
        "reference_score": sum(row["status"] == "pass" for row in reference_rows) / len(reference_rows),
        "initial_checks": noop,
        "reference_checks": reference_rows,
        "at": now(),
    }
    if golden:
        report.update(golden_sid=golden_sid, contributions=contributions)
    write(folder / "runtime" / "grades" / task_id / "calibration.json", report)
    if unavailable:
        raise CalibrationUnavailable("; ".join(unavailable))
    if not accepted:
        raise CalibrationError(
            "grader rejected: EVERY state check must meet calibration; " + "; ".join(failures)
        )
    return report
