"""Bind workers to the apps their VMs hold, per task, from the accepted task text.

Stage 1 contributions said what each worker does but not which system they do it in, so
seeding gave every worker every app and nothing forced delegation across VMs. This pass asks
the company-worker-apps skill for the binding once per company, checks it against the
company's apps and record collections, and writes it into each task's contributions. The
seeder then derives worker_apps from the contract instead of guessing.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from company_envs.config import load_config
from company_envs.models import ModelOutputInvalid, Models
from company_envs.receipt import Receipt
from company_envs.storage import digest, now, read, write

from .barrier import SPREADSHEET_APP
from .hub_app import record_collections
from .state_seed import STANDARD_APPS, load_skill

SKILL = "company-worker-apps"


class WorkerBinding(BaseModel):
    worker_id: str
    apps: list[str]


class TaskBinding(BaseModel):
    workflow_id: str
    decisive_collections: list[str]
    contributions: list[WorkerBinding]


class WorkerAppsResult(BaseModel):
    tasks: list[TaskBinding]


def company_apps(root, folder):
    apps = read(folder / "apps.json")["apps"]
    out = {}
    for app in apps:
        text = (root / app["schema"]).read_text() if (root / app["schema"]).is_file() else ""
        out[app["app_id"]] = record_collections(text) or app.get("top_level_keys") or []
    return out


def systems_of_record(apps):
    """Domain apps a decisive collection can sit in, preferring real ones over the spreadsheet.

    Everyone in an office has a spreadsheet, so gating a task on who holds Sheets is no barrier.
    Thirteen companies have nothing else and this stage cannot give them one, so the spreadsheet
    stays eligible exactly when no other domain app carries a record collection.
    """
    domain = {a for a in apps if a not in STANDARD_APPS}
    return {a for a in domain if a != SPREADSHEET_APP and apps[a]} or domain


def company_grants(folder):
    """The company-wide worker -> app grant a world must be seeded against, or None when unbound.

    One rule, one place, derived from the tasks on disk: the union of every task's contributions,
    narrowed to the apps the company declares and widened by the standard bundle everybody gets,
    over every worker on the roster -- including a worker no task uses, who still has a mailbox.

    This is the last word on who can log in to what, and it is settled here, before the world is
    written: the binding stage runs ahead of seeding (ASSIGN.json is the seed stage's input), so the
    seeder has no reason to guess and the check stage has nothing left to narrow. Three copies of
    this rule exist -- state_seed.contract_worker_apps, validate_core's contract branch and
    sync_worker_apps -- and they disagree on the edges: sync_worker_apps narrows only the workers
    the world already listed, so a worker the seeder left out stays out, and none of them intersect
    with the declared apps before handing the list to an app author.

    Returning None means no task names an app at all, which is a company whose binding was never
    written; an empty list for one worker is a real grant of the standard bundle alone. Consumers
    derive this from the tasks rather than reading it out of ASSIGN.json, because a task mined after
    the company-wide pass deliberately does not re-date that marker (re-dating it re-seeds the
    world), so the marker's copy of the map can be older than the tasks.
    """
    folder = Path(folder)
    manifest = read(folder / "apps.json")
    declared = {app["app_id"] for app in manifest["apps"]}
    roster = list(manifest.get("workers") or [w["id"] for w in read(folder / "company.json")["workers"]])
    bound, named = {}, False
    for path in sorted(folder.glob("tasks/*/workflow.json")):
        if path.parent.name.startswith("_"):
            continue
        for contribution in read(path).get("contributions", []):
            apps = contribution.get("apps")
            if apps is None:
                return None
            named = named or bool(apps)
            bound.setdefault(contribution["worker_id"], set()).update(apps)
    if not named:
        return None
    standard = STANDARD_APPS & declared
    return {
        worker: sorted((bound.get(worker, set()) & declared) | standard)
        for worker in dict.fromkeys([*roster, *bound])
    }


def check_binding(workflow, binding, apps):
    problems = []
    workers = [c["worker_id"] for c in workflow.get("contributions", [])]
    bound = {b.worker_id: b.apps for b in binding.contributions}
    if set(bound) != set(workers):
        problems.append(f"contributions must cover exactly these workers: {workers}")
    domain = {a for a in apps if a not in STANDARD_APPS}
    records = systems_of_record(apps)
    for worker, held in bound.items():
        unknown = set(held) - set(apps)
        if unknown:
            problems.append(f"{worker} holds apps the company does not have: {sorted(unknown)}")
    if domain and bound and all(domain <= set(held) for held in bound.values()):
        problems.append(
            "every worker holds every domain app; at least one must lack one so the work is delegated"
        )
    cells = binding.decisive_collections
    manager = workflow.get("manager_id")
    reach = {w: set(held) | STANDARD_APPS for w, held in bound.items()}
    holders = {
        c: sorted(w for w, apps_held in reach.items() if c.split(".")[0] in apps_held)
        for c in cells
        if c.split(".")[0] not in STANDARD_APPS
    }
    # A collection nobody holds passes every other rule here: the manager is kept out of it, so it
    # reads as the access barrier that makes the task need delegation, while no worker can reach the
    # records either and the delegation cannot happen. Nothing downstream notices -- the world seeds
    # the app, the grader reads the collection, and the episode has no one who can write it. None of
    # the 178 bindings on disk do this; the gate is here so the latent case stays latent.
    for cell, who in sorted(holders.items()):
        if not who and cell.partition(".")[0] in apps:
            problems.append(
                f"{cell} is held by nobody; give {cell.partition('.')[0]} to the worker whose "
                "contribution produces that finding"
            )
    if manager in bound and domain and cells:
        gating = sorted(c for c, who in holders.items() if manager not in who)
        if not gating:
            problems.append(
                f"the manager ({manager}) holds every app the decisive collections live in and could do the task alone; "
                f"put a decisive collection in one of {sorted(records)} that the manager does not hold"
            )
        elif not any(c.split(".")[0] in records for c in gating):
            problems.append(
                f"only {sorted({c.split('.')[0] for c in gating})} keeps the manager ({manager}) out, and a "
                f"shared spreadsheet is not a system of record; move a decisive collection into one of "
                f"{sorted(records)} that the manager does not hold"
            )
        for cell, who in sorted(holders.items()):
            if who == [manager]:
                problems.append(
                    f"{cell} is held by the manager ({manager}) alone; give that app to the specialist "
                    "whose finding the manager has to wait for"
                )
    if not 1 <= len(cells) <= 2:
        problems.append("decisive_collections must name one or two app.collection entries")
    for cell in cells:
        app, _, key = cell.partition(".")
        if app not in apps or key not in apps[app]:
            problems.append(f"{cell!r} is not a record collection of the company's apps")
    if domain and cells and not any(c.split(".")[0] in domain for c in cells):
        problems.append("one decisive collection must come from a domain app")
    return problems


def assign_worker_apps(root, folder, models=None, again=False, only=None):
    """Bind every task of the company, or, with ONLY, the named ids alone.

    ONLY is the mined-task path: tasks authored after the company-wide pass have no binding, and
    the pass itself will not run again because ASSIGN.json is there. Naming ids binds just those
    (skipping any already bound) and leaves ASSIGN.json untouched -- it is the seed stage's input,
    so re-dating it would re-seed the accepted world the new tasks were authored against.
    """
    root, folder = Path(root), Path(folder)
    marker = folder / "tasks" / "_worker_apps" / "ASSIGN.json"
    if only is None and marker.is_file() and not again:
        # Nothing ran, and that is a pass: the binding this marker names is on disk. ``tasks: 0``
        # with a bare ``skipped`` is the vocabulary company_envs.receipt exists to retire -- it is
        # the shape a run that bound nothing would have, and a reader had to know this stage to tell.
        return {**Receipt.passed(tasks=0).body, "skipped": "already assigned"}
    config = load_config(root)
    models = models if models is not None else Models(config, folder / "tasks" / "_worker_apps")
    instructions, skill = load_skill(root, SKILL)
    company = read(folder / "company.json")
    apps = company_apps(root, folder)
    records = systems_of_record(apps)
    workflows = {
        p.parent.name: read(p)
        for p in sorted((folder / "tasks").glob("*/workflow.json"))
        if not p.parent.name.startswith("_")
    }
    if only is not None:
        unknown = sorted(set(only) - set(workflows))
        if unknown:
            raise ValueError(f"{folder} has no such tasks: {unknown}")
        workflows = {w: t for w, t in workflows.items() if w in set(only) and "worker_apps" not in t}
        if not workflows:
            return {**Receipt.passed(tasks=0).body, "skipped": "already assigned"}
    if not workflows:
        raise ValueError(f"{folder} has no tasks")
    payload = {
        "call": "worker_apps",
        "company": {k: company.get(k) for k in ("name", "sector", "operations")},
        # The roster already says what each person can reach; title alone invites guesswork.
        "workers": [
            {k: w.get(k) for k in ("id", "title", "information_access")} for w in company.get("workers", [])
        ],
        "apps": [
            {
                "app_id": a,
                "record_collections": keys,
                "standard": a in STANDARD_APPS,
                "system_of_record": a in records,
            }
            for a, keys in apps.items()
        ],
        "tasks": [
            {
                "workflow_id": wid,
                "title": w["title"],
                "brief": w["brief"],
                "manager_id": w.get("manager_id"),
                # Only the ones the model may keep. The matrix draws a cell from every top-level
                # key, so 92 of 178 pairs arrived carrying UI state the binding has to replace
                # anyway; handing those over invites a copy and costs a revision round.
                "current_decisive_collections": [
                    c
                    for c in (w.get("feature_cell") or {}).get("collections", [])
                    if c.partition(".")[2] in apps.get(c.partition(".")[0], [])
                ],
                "contributions": [
                    {"worker_id": c["worker_id"], "work": c["work"]} for c in w.get("contributions", [])
                ],
            }
            for wid, w in workflows.items()
        ],
    }
    feedback = None
    for attempt in range(3):
        prompt = (
            instructions
            + "\n"
            + json.dumps(
                {**payload, **({"revision_feedback": feedback} if feedback else {})}, ensure_ascii=False
            )
        )
        result, receipt = models.call("expand", prompt, WorkerAppsResult)
        by_id = {t.workflow_id: t for t in result.tasks}
        if set(by_id) != set(workflows):
            problems = ["return every task exactly once with its workflow_id unchanged"]
        else:
            problems = [
                f"{wid}: {p}" for wid, w in workflows.items() for p in check_binding(w, by_id[wid], apps)
            ]
        if not problems:
            break
        feedback = problems
    else:
        raise ModelOutputInvalid("; ".join(problems[:8]), result.model_dump(), receipt)
    for wid, w in workflows.items():
        binding = by_id[wid]
        bound = {b.worker_id: sorted(set(b.apps)) for b in binding.contributions}
        for contribution in w.get("contributions", []):
            contribution["apps"] = bound[contribution["worker_id"]]
        cell = w.get("feature_cell") or {}
        if sorted(binding.decisive_collections) != sorted(cell.get("collections", [])):
            w.setdefault("feature_cell_before", cell)
            w["feature_cell"] = {
                "collections": sorted(binding.decisive_collections),
                "decision_type": cell.get("decision_type"),
            }
        w["worker_apps"] = {"assigned_at": now(), "skill": skill}
        write(folder / "tasks" / wid / "workflow.json", w)
    if only is None:
        # ``passed``, and it is the only outcome this marker can carry, which is the finding worth
        # recording: ASSIGN.json was scoped as the clearest remaining instance of "an empty run
        # writes the receipt a real verdict writes", and measuring says it is not one. A company
        # with no tasks raises above, and a binding that does not cover every task raises
        # ModelOutputInvalid after three attempts, so the marker exists only after a full bind --
        # 0 of the 98 ASSIGN.json on disk hold ``tasks: []`` or ``attempts: 0`` (2026-09-10).
        #
        # The outcome is written anyway, and the test beside it pins the invariant, because today
        # that guarantee is two raises thirty lines apart and nothing fails if one is removed.
        #
        # ``tasks`` is what this company-wide pass bound, not every bound task: the --only mined
        # path deliberately leaves this marker alone, so the list can be shorter than the folder's
        # tasks. 1 of the 98 on disk is (2026-09-10), and that is intent, not drift.
        write(
            marker,
            {
                "assigned_at": now(),
                **Receipt.passed().body,
                "skill": skill,
                "receipt": receipt,
                "attempts": attempt + 1,
                "tasks": sorted(workflows),
            },
        )
    manifest = read(folder / "MANIFEST.json")
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes()) for p in sorted(folder.rglob("*")) if p.is_file()
    }
    write(folder / "MANIFEST.json", manifest)
    # The grant the world will be seeded against, as the binding just settled it. Printed rather
    # than filed: ASSIGN.json is not rewritten on the mined-task path, so a copy stored there could
    # be older than the tasks, and every consumer can derive this in a millisecond from the folder.
    return {
        **Receipt.passed().body,
        "tasks": len(workflows),
        "attempts": attempt + 1,
        "grants": company_grants(folder),
    }
