"""Frozen, provenance-checked history and bounded substantive planning context."""

import json
from collections import Counter, defaultdict
from pathlib import Path

from .schemas import Company, Workflow
from .storage import digest, read, write


def cell_key(cell):
    """Readable, order-independent JSON histogram key, without delimiter collisions."""
    return json.dumps(
        [sorted(cell["collections"]), cell["decision_type"]], ensure_ascii=False, separators=(",", ":")
    )


def cell_histogram(workflows):
    """Count all supplied accepted tasks, independent of the bounded prose sample."""
    counts = Counter()
    for workflow in workflows:
        raw = workflow if isinstance(workflow, dict) else workflow.model_dump()
        if cell := raw.get("feature_cell"):
            counts[cell_key(cell)] += 1
    return dict(sorted(counts.items()))


def freeze_history(root, run_dir):
    """Snapshot accepted local history once; never read live runs during resume.

    This is the known local corpus, not a claim of global novelty. Keep one latest
    accepted version per workflow identity so repeated saved versions do not fill
    the retrieval budget. Invalid provenance is recorded, not silently trusted.
    """
    from .report import verified_entries

    root, run_dir = Path(root).resolve(), Path(run_dir).resolve()
    latest, quarantined, companies, assignments = {}, [], {}, {}
    for path in sorted((root / "runs").glob("*/run.json")):
        if path.parent.resolve() == run_dir:
            continue
        try:
            state = read(path)
            if not state.get("entries"):
                continue
            entries, errors = verified_entries(root, path.parent, state)
            quarantined.extend(
                {"run": str(path.parent.relative_to(root)), "workflow_id": key, "reason": value}
                for key, value in sorted(errors.items())
            )
            for entry in entries:
                if entry["verdict"] != "accept":
                    continue
                raw = read(root / entry["workflow_path"])
                workflow = Workflow.model_validate(raw)
                if Path(entry["workflow_path"]).stem != digest(raw) or workflow.id != entry["workflow_id"]:
                    raise ValueError("historical workflow changed while freezing portfolio")
                key = (workflow.company_id, workflow.id)
                rank = (state.get("created_at", path.parent.name), path.parent.name, digest(raw))
                row = {
                    "workflow": raw,
                    "content_hash": digest(raw),
                    "origin": {
                        "run": str(path.parent.relative_to(root)),
                        "workflow_id": workflow.id,
                        "review_path": entry["review_path"],
                        "review_input_hash": entry["review_input_hash"],
                        "family_id": entry["family_id"],
                    },
                }
                if entry.get("company_path"):
                    raw_company = read(root / entry["company_path"])
                    company = Company.model_validate(raw_company)
                    if (
                        Path(entry["company_path"]).stem != digest(raw_company)
                        or company.id != workflow.company_id
                    ):
                        raise ValueError("historical company changed while freezing portfolio")
                    if company.id not in companies or rank > companies[company.id][0]:
                        companies[company.id] = (rank, raw_company)
                if key not in latest or rank > latest[key][0]:
                    latest[key] = (rank, row)
            for row in assigned_app_history(state.get("jobs", {}), path.parent):
                assignments[(path.parent.name, row["job_id"])] = {"run_id": path.parent.name, **row}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            quarantined.append({"run": str(path.parent.relative_to(root)), "reason": str(exc)})
    # Byte-identical content copied between runs is one comparison, not many.
    rows = {row["content_hash"]: row for _, row in latest.values()}
    snapshot = {
        "schema_version": "1",
        "scope": "Provenance-checked accepted workflows from local runs at creation time; latest accepted version per identity.",
        "workflows": [rows[key] for key in sorted(rows)],
        "quarantined": quarantined,
        "companies": [row for _, row in sorted(companies.values(), key=lambda item: item[1]["id"])],
        "assigned_app_history": [assignments[key] for key in sorted(assignments)],
    }
    write(run_dir / "portfolio.json", snapshot)
    return {
        "path": "portfolio.json",
        "hash": digest(snapshot),
        "count": len(rows),
        "quarantined": len(quarantined),
    }


def load_history(run_dir, state):
    """Read only the frozen snapshot, with collision-safe IDs for review links."""
    manifest = state.get("portfolio")
    if manifest is None:
        return []  # Historical runs retain their original comparison scope.
    run_dir = Path(run_dir).resolve()
    path = (run_dir / manifest["path"]).resolve()
    if not path.is_relative_to(run_dir):
        raise ValueError("portfolio path escapes run directory")
    snapshot = read(path)
    if digest(snapshot) != manifest["hash"]:
        raise ValueError("frozen portfolio changed")
    if len(snapshot["workflows"]) != manifest["count"]:
        raise ValueError("frozen portfolio count changed")
    result = []
    for row in snapshot["workflows"]:
        if digest(row["workflow"]) != row["content_hash"]:
            raise ValueError("frozen portfolio workflow changed")
        workflow = Workflow.model_validate(row["workflow"])
        result.append(workflow.model_copy(update={"id": "history-" + row["content_hash"]}))
    return result


def company_histograms(companies=(), *, surface=(), sectors=()):
    """Count accepted dossiers once per company/app, including unused surface apps.

    Callers supply accepted companies, not drafted dossiers or selected task counts.
    Workflows alone do not contain a company's sector or full software inventory.
    """
    unique = {company.id: company for company in companies}
    apps = Counter({app_id: 0 for app_id in surface})
    counts = Counter({sector: 0 for sector in sectors})
    allowed = set(surface)
    for company in unique.values():
        counts[company.sector] += 1
        apps.update(
            {
                app_id
                for software in company.software
                for app_id in software.catalog_app_ids
                if app_id in allowed
            }
        )
    return {"app_usage": dict(sorted(apps.items())), "sector_counts": dict(sorted(counts.items()))}


def assigned_app_history(jobs, run_dir=None):
    """Reservations and latest research decisions, including declines and failed jobs."""
    rows = []
    for job_id, job in sorted(jobs.items()):
        if job.get("reuse_dossier"):
            continue
        assigned = job.get("coverage", {}).get("assigned_apps")
        if assigned is None:
            continue
        row = {
            "job_id": job_id,
            "company_id": job["candidate"]["id"],
            "sector": job["candidate"]["sector"],
            "status": job["status"],
            "assigned_apps": list(assigned),
        }
        if run_dir is not None:
            from .provenance import author_receipts

            for receipt in author_receipts(Path(run_dir) / "jobs" / job_id):
                if decision := receipt.get("app_assignment"):
                    row.update(decision)
        rows.append(row)
    return rows


def history_app_context(run_dir, state):
    """Read accepted dossiers from the same frozen snapshot as workflow history."""
    if "portfolio" not in state:
        return [], []
    load_history(run_dir, state)  # Reuse path, hash and workflow integrity checks.
    snapshot = read(Path(run_dir) / state["portfolio"]["path"])
    return (
        [Company.model_validate(raw) for raw in snapshot.get("companies", [])],
        snapshot.get("assigned_app_history", []),
    )


def in_flight_app_usage(jobs, accepted_ids=(), run_dir=None):
    reservations = defaultdict(set)
    for job in jobs.values():
        cid = job.get("candidate", {}).get("id")
        if cid in accepted_ids or job.get("reuse_dossier"):
            continue
        if job["status"] in ("queued", "running", "drafted", "pending_model", "error"):
            reservations[cid].update(job.get("coverage", {}).get("assigned_apps", []))
            if run_dir is not None:
                path = Path(run_dir) / "jobs" / job["id"] / "company.json"
                if path.is_file():
                    company = Company.model_validate(read(path))
                    reservations[cid].update(
                        app for software in company.software for app in software.catalog_app_ids
                    )
    counts = Counter(app for apps in reservations.values() for app in apps)
    return dict(sorted(counts.items()))


def summarize(workflows, limit=24, *, include_cells=True):
    """Bound planning context, preserving work rather than occupational labels.

    Company-balanced deterministic sampling is context selection, not a task quota
    or proof of diversity. Review retrieval still searches the full frozen corpus.

    The app, sector and cell counters live beside this summary in the coverage
    payload, not inside it. Repeating them here put 11 KB of byte-identical JSON
    (62 KB in the widest run) into every research, design and review prompt.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("portfolio summary limit must be a nonnegative integer")
    groups = defaultdict(list)
    unique = {digest(w.model_dump()): w for w in workflows}
    for key, workflow in sorted(unique.items()):
        groups[workflow.company_id].append((key, workflow))
    ordered = []
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for company_id in sorted(groups, key=digest):
            if index < len(groups[company_id]):
                ordered.append(groups[company_id][index][1])

    def clip(text, size=220):
        return text if len(text) <= size else text[: size - 1] + "…"

    work = []
    for workflow in ordered[:limit]:
        constraints = (
            workflow.completion.feasible_path.constraint_checks
            if workflow.completion is not None
            else workflow.initial_materials
        )
        work.append(
            {
                "workflow_id": workflow.id,
                "company_id": workflow.company_id,
                "decision_problem": clip(workflow.decision_problem),
                "contributions": [clip(c.work) for c in workflow.contributions[:4]],
                "decisions_and_effects": [
                    {"decision": clip(p.decision), "effect": clip(p.downstream_effect)}
                    for p in workflow.phases[:3]
                ],
                "constraints_or_initial_context": [clip(c) for c in constraints[:3]],
                "deliverables": [clip(d) for d in workflow.deliverables[:3]],
                **(
                    {"feature_cell": workflow.model_dump()["feature_cell"]}
                    if include_cells and workflow.model_dump().get("feature_cell")
                    else {}
                ),
            }
        )
    return {
        "total_workflows": len(unique),
        "shown_workflows": len(work),
        "selection": "Deterministic company-balanced sample; fields are excerpts, not exhaustive task specifications.",
        "work": work,
    }
