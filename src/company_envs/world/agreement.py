"""One file per company that says whether every gate holds: AGREEMENT.json.

CUA-Gym's orchestrator declares its agreement conditions and a task is done when all hold
under execution. This is the same idea for a company: each condition is a named check with the
evidence it read, and ``done`` is true only when all of them are true. The batch driver marks
a company complete on this file, and the cohort sheet shows it.
"""

import collections
import json
from pathlib import Path

from company_envs.storage import decided_by, now, read, write
from company_envs.workflows import RELATIVE_TIME

from .barrier import check_barrier
from .controller import task_runtime
from .state_seed import STANDARD_APPS, contract_worker_apps

CONDITIONS = (
    "seeded",
    "checks",
    "reviewed",
    "readable",
    "calibrated",
    "barrier",
    "evergreen",
    "vm_verified",
    "roles_used",
)


def _read(path):
    try:
        return read(path)
    except (OSError, ValueError):
        return None


RETIRED_MARKERS = ("CALIBRATION_FAILED.json", "GRADER_FAILED.json", "GOLDEN_FAILED.json")
# The artifact that revives a retired task: the thing the refusal said could not be written, written
# after it. The same pair the batch driver keeps, and it has to stay the same pair -- a task the
# driver has retired but the agreement still counts is a company that can never finish.
# rainbow-shops holds an accepted all-checks calibration on one of its two tasks and was held out of
# delivery by the other task's golden author, which returns ``'p31'`` where an object is required.
REVIVED_BY = {"GRADER_FAILED.json": "grader.json", "GOLDEN_FAILED.json": "golden.json"}


def _retired(task, folder):
    """Whether this task is finished with, and no later accepted calibration revived it.

    The same rule the batch driver applies when it decides a company still has work left. The
    driver stopped retiring a whole company for one ungradeable task; without this the agreement
    would still read that task, never find a teacher pass for it, and fail the company anyway.
    """
    for marker in RETIRED_MARKERS:
        stamp = task / marker
        if not stamp.is_file():
            continue
        if (revives := REVIVED_BY.get(marker)) and (task / revives).is_file():
            continue
        proof = folder / "runtime" / "grades" / task.name / "calibration.json"
        if proof.is_file() and proof.stat().st_mtime > stamp.stat().st_mtime:
            report = _read(proof) or {}
            if report.get("accepted") and report.get("scope") == "all_checks":
                continue
        return True
    return False


def _all_tasks(folder):
    return [
        p.parent for p in sorted(folder.glob("tasks/*/workflow.json")) if not p.parent.name.startswith("_")
    ]


def _tasks(folder):
    """The tasks this company is still delivering; a retired one is not held against it."""
    return [t for t in _all_tasks(folder) if not _retired(t, folder)]


def _read_bus(runtime, folder, task):
    """The run's message log, wherever the controller left it; None when there is none to read."""
    for path in (
        runtime / "controller/company/runtime/messages.jsonl",
        folder / "runtime/teacher" / task / "messages.jsonl",
    ):
        try:
            return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            continue
    return None


def roles_used(report, messages=None):
    """Which roster members did work a check reads, and which took part in the coordination.

    ``changes`` on a worker's contribution carries the ids of the checks its records feed, so a
    worker whose changes no check cites did nothing the task is graded on. Messages come from the
    run's own bus log. A worker is idle when it changed nothing a check reads, or when nobody ever
    wrote to it, or it never wrote to anyone.
    """
    contributions = (report or {}).get("contributions") or {}
    workers = contributions.get("workers") or []
    if not workers:
        return {"judged": False, "idle": [], "detail": {}}
    sent, received = collections.Counter(), collections.Counter()
    for row in messages or []:
        if isinstance(row, dict) and row.get("event") == "send":
            sent[row.get("sender")] += 1
            received[row.get("recipient")] += 1
    detail, idle = {}, []
    for worker in workers:
        name = worker.get("worker_id")
        cited = sorted({c for ch in worker.get("changes") or [] for c in ch.get("check_ids") or []})
        row = {"checks_fed": cited, "sent": sent.get(name, 0), "received": received.get(name, 0)}
        detail[name] = row
        if not cited or (messages is not None and not (row["sent"] and row["received"])):
            idle.append(name)
    return {"judged": True, "idle": sorted(idle), "detail": detail}


# What decides a delivery verdict: these conditions. The same file is scripts/batch_companies.py's
# AGREEMENT_CODE, and the digest recorded in an AGREEMENT.json is compared against this tuple, so the
# two have to name the same files. This is the marker a wrongly-fresh verdict costs the most on: it
# is what says a company counts towards the target, and it took 13 of the 295 commits in the week to
# 2026-09-10 while nothing on disk could re-date it.
DECIDES_THIS = (Path(__file__),)


def current(marker):
    """Whether an AGREEMENT.json was decided by these conditions as they stand now.

    The driver's accept slot, bulk_layer.current's contract. A marker with no ``decided_by`` is stale
    -- both of the two on disk on 2026-09-10, each 64 s to re-take and with nothing under it.
    """
    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


def agreement(folder):
    folder = Path(folder)
    seed = _read(folder / "world" / "SEED.json") or {}
    checks = _read(folder / "world" / "CHECKS.json") or {}
    readability = _read(folder / "REPORT-readability.json") or {}
    tasks = _tasks(folder)
    result = {}

    result["seeded"] = {"ok": bool(seed), "evidence": seed.get("status")}
    # A check counts only when it covered every app the company has; an empty check is no check.
    app_ids = sorted(a["app_id"] for a in (_read(folder / "apps.json") or {}).get("apps", []))
    covered = sorted(checks.get("apps_checked") or [])
    result["checks"] = {
        "ok": bool(checks) and checks.get("ok") is True and bool(app_ids) and covered == app_ids,
        "evidence": {
            "errors": checks.get("errors"),
            "warnings": checks.get("warnings"),
            "apps_checked": covered,
        },
    }
    result["reviewed"] = {"ok": seed.get("status") == "seeded_reviewed", "evidence": seed.get("review")}

    categories = readability.get("categories") or {}
    verdicts = {
        name: (cat.get("verdict") if isinstance(cat, dict) else cat) for name, cat in categories.items()
    }
    # The report decides what blocks, rather than this inferring it from every category's verdict.
    # Inferring held a company back on a material or message reading nothing can repair -- 45 of 48
    # reports failed as written and 14 of the 17 that fail today do so on materials alone, while the
    # prose the pipeline actually rewrites is the brief. A report without the field is read the old
    # way, so an older marker keeps its meaning.
    blocking = readability.get("blocking")
    result["readable"] = {
        "ok": not blocking
        if blocking is not None
        else (bool(verdicts) and verdicts.get("brief") == "plain" and "system-log" not in verdicts.values()),
        "evidence": {"verdicts": verdicts, "blocking": blocking},
    }

    calibrations = {}
    for task in tasks:
        proof = _read(folder / "runtime" / "grades" / task.name / "calibration.json") or {}
        # Full calibration: every check, judged ones included, proven on the golden and the
        # untouched initial state. A state-only proof is partial and does not count.
        calibrations[task.name] = {"accepted": proof.get("accepted"), "scope": proof.get("scope")}
    result["calibrated"] = {
        "ok": bool(tasks)
        and all(v["accepted"] is True and v["scope"] == "all_checks" for v in calibrations.values()),
        "evidence": calibrations,
    }

    contract = contract_worker_apps(folder)
    barrier = check_barrier(folder)
    solo = sorted({f["task"] for f in barrier["findings"] if f["detail"].get("manager_could_finish_alone")})
    world_apps = _read(folder / "world" / "worker_apps.json")
    synced = False
    if contract and world_apps:
        apps_here = {a["app_id"] for a in (_read(folder / "apps.json") or {}).get("apps", [])}
        synced = set(contract) <= set(world_apps) and all(
            set(world_apps[w]) == (set(contract.get(w, [])) | STANDARD_APPS) & apps_here for w in world_apps
        )
    result["barrier"] = {
        "ok": contract is not None and synced and barrier["ok"],
        "evidence": {
            "contract": contract is not None,
            "manager_could_finish_alone": solo,
            "world_worker_apps_synced": synced,
            "record_barrier": barrier,
        },
    }

    relative = {}
    for task in tasks:
        workflow = _read(task / "workflow.json") or {}
        hits = RELATIVE_TIME.findall(str(workflow.get("brief", "")))
        if hits:
            relative[task.name] = sorted({h.lower() for h in hits})
    result["evergreen"] = {
        "ok": bool(seed.get("reference_date")) and not relative,
        "evidence": {"reference_date": seed.get("reference_date"), "relative_time_in_briefs": relative},
    }

    # Proof under execution: every task ran on real worker VMs (launch, teacher, one episode and
    # grade per worker policy, teardown) in its own runtime, the insider-access teacher passed
    # the grader on the live apps for every task, and no policy run of any task hit an
    # environment-class fault (a wiped collection, an unavailable judge, an episode that ended
    # in error).
    episodes, runs_by_task = {}, {}
    for task in tasks:
        runtime = task_runtime(folder, task.name)
        controller = _read(runtime / "CONTROLLER.json") or {}
        options = controller.get("options") or {}
        policy_runs = controller.get("policy_runs") or {}
        runs_by_task[task.name] = policy_runs
        # The controller runs in its own working copy of the company; the teacher report lands
        # there unless it was copied back.
        report = (
            _read(folder / "runtime" / "teacher" / task.name / "TEACHER.json")
            or _read(runtime / "controller" / "company" / "runtime" / "teacher" / task.name / "TEACHER.json")
            or {}
        )
        episodes[task.name] = {
            "roles": roles_used(report, _read_bus(runtime, folder, task.name)),
            "class": report.get("class"),
            "score": report.get("score"),
            "backend": options.get("backend"),
            "real_backend": options.get("backend") == "mypcbench" and not options.get("dry_run"),
            "controller_status": controller.get("status"),
            "policy_runs": policy_runs,
            "environment_faults": sorted(
                p for p, run in policy_runs.items() if isinstance(run, dict) and run.get("environment_fault")
            ),
        }
    faults = {t: e["environment_faults"] for t, e in episodes.items() if e["environment_faults"]}
    idle = {t: e["roles"]["idle"] for t, e in episodes.items() if e["roles"]["idle"]}
    result["roles_used"] = {
        # A task that one or two people could finish is a single-agent task with spectators. Every
        # person on the roster has to have changed something a check reads, and to have both sent
        # and received a message: work and coordination, not attendance.
        "ok": bool(tasks) and not idle and all(e["roles"]["judged"] for e in episodes.values()),
        "evidence": {"idle": idle, "per_task": {t: e["roles"] for t, e in episodes.items()}},
    }
    result["vm_verified"] = {
        "ok": bool(tasks)
        and all(e["real_backend"] for e in episodes.values())
        and all(e["class"] == "teacher_passed" for e in episodes.values())
        and not faults,
        "evidence": {
            "backends": {t: e["backend"] for t, e in episodes.items()},
            "episodes": episodes,
            "policy_runs": runs_by_task,
            "environment_faults": faults,
        },
    }

    report = {
        "checked_at": now(),
        "conditions": result,
        "decided_by": decided_by(DECIDES_THIS),
        "difficulty": difficulty(merge_policy_runs(runs_by_task)),
        "done": all(result[c]["ok"] for c in CONDITIONS),
    }
    report["difficulty"]["by_task"] = {t: difficulty(runs)["label"] for t, runs in runs_by_task.items()}
    write(folder / "AGREEMENT.json", report)
    return report


def merge_policy_runs(runs_by_task):
    """One row per policy across a company's tasks: passed only when it passed every task it was
    graded on, failed when it failed any, ungraded when no task graded it."""
    merged = {}
    for runs in runs_by_task.values():
        for policy, run in runs.items():
            if not isinstance(run, dict):
                continue
            row = merged.setdefault(policy, {"passed": None})
            if run.get("passed") is False or row["passed"] is False:
                row["passed"] = False
            elif run.get("passed") is True:
                row["passed"] = True
    return merged


def difficulty(policy_runs):
    """Which no-hint worker policies passed the graded episode. Informational, never a gate:
    "easy" when every graded policy passed, "hard" when none did, "medium" otherwise, and no
    label at all when nothing was graded (a skipped or dry run)."""
    graded = {p: run for p, run in policy_runs.items() if isinstance(run, dict)}
    passed_by = sorted(p for p, run in graded.items() if run.get("passed") is True)
    failed_by = sorted(p for p, run in graded.items() if run.get("passed") is False)
    if not passed_by and not failed_by:
        label = None
    elif not failed_by:
        label = "easy"
    elif not passed_by:
        label = "hard"
    else:
        label = "medium"
    return {"passed_by": passed_by, "failed_by": failed_by, "label": label}
