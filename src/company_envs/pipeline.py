"""Resumable coordinator. Authors and reviewers write only isolated job directories."""

import copy
import json
import math
import shutil
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlparse

from company_envs.config import load_config

from . import research as research_job
from . import workflows as workflow_job
from .app_affinity import app_coverage, assign_apps, target_apps, under_used_app_categories
from .catalogs import Catalogs, sector_targets
from .diversity import Embeddings, nearest, select
from .holdout import overlap
from .models import (
    MODEL_ADAPTER_VERSION,
    ModelOutputInvalid,
    Models,
    ModelUnavailable,
    cancel_calls,
    reset_cancellation,
)
from .portfolio import (
    assigned_app_history,
    cell_histogram,
    company_histograms,
    freeze_history,
    history_app_context,
    in_flight_app_usage,
    load_history,
    summarize,
)
from .provenance import author_receipts, record_author, validate_separation
from .review import apply_brief_fixes, evidence_errors, review
from .schemas import Company, Workflow, minimum_workers, validate_workflow
from .skills import freeze_skills
from .sources import Sources
from .storage import bound_review, digest, now, read, run_lock, write
from .world.capabilities import runtime_adapter
from .world.hub_app import hub_apps


class DesignRejected(ValueError):
    pass


def model_failure(state, job_id, exc):
    """Checkpoint a bounded job retry; return whether the whole invocation must stop."""
    job = state["jobs"][job_id]
    generation = state["config"]["generation"]
    failures = job.get("model_failures", 0) + 1
    stage = "review" if job.get("status") == "drafted" else job.get("retry_stage", "prepare")
    isolated = generation.get("isolate_model_failures", False)
    retry = (
        isolated
        and exc.retryable
        and not exc.global_failure
        and failures <= generation.get("max_model_retries", 0)
    )
    job.setdefault("model_failure_history", []).append(
        {
            "at": now(),
            "error": str(exc),
            "retryable": exc.retryable,
            "global_failure": exc.global_failure,
            "scheduled_retry": retry,
            "stage": stage,
        }
    )
    job.update(
        status=("drafted" if stage == "review" else "queued") if retry else "pending_model",
        error=str(exc),
        model_failures=failures,
        retry_stage=stage,
    )
    stop = not isolated or exc.global_failure
    if stop:
        state["error"] = str(exc)
    return stop


def job_failure(state, job_id, exc):
    """Record worker or coordinator-stage failures only on the coordinator."""
    if isinstance(exc, ModelUnavailable):
        return model_failure(state, job_id, exc)
    if isinstance(exc, DesignRejected):
        state["jobs"][job_id].update(status="rejected", error=str(exc))
        return False
    state["jobs"][job_id].update(status="error", error=f"{type(exc).__name__}: {exc}")
    state["error"] = f"{type(exc).__name__}: {exc}"
    return True


class WorkerPool(ThreadPoolExecutor):
    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
            cancel_calls()
        return super().__exit__(exc_type, exc_value, traceback)


def new_run(root, company_target=None, task_target=None, workers=None, seed=None):
    config = load_config(root)
    design = config.get("design", {})
    if not isinstance(design, dict):
        raise ValueError("design must be a table")  # noqa: TRY004 - invalid user configuration
    if type(design.get("feature_matrix", True)) is not bool:
        raise ValueError("design.feature_matrix must be a boolean")
    config.setdefault("design", design).setdefault("feature_matrix", True)
    worker_floor = minimum_workers(config)
    version = design.get("workflow_version", "1")
    if version not in ("1", "2"):
        raise ValueError("workflow_version must be 1 or 2")
    mode = design.get("execution_mode", "scheduled")
    if mode not in ("scheduled", "digital"):
        raise ValueError("execution_mode must be scheduled or digital")
    if mode == "digital" and (version != "2" or worker_floor < 3):
        raise ValueError("digital execution_mode requires workflow_version 2 and minimum_workers at least 3")
    if "available_runtime_apps" in design:
        apps = design["available_runtime_apps"]
        if (
            not isinstance(apps, list)
            or not apps
            or any(not isinstance(app, str) or not app for app in apps)
            or len(set(apps)) != len(apps)
        ):
            raise ValueError("available_runtime_apps must be a nonempty list of unique catalog app IDs")
        if runtime_adapter(config) == "desktop":
            # The desktop surface is applications, not web apps: its ids live in their own
            # catalogue, and checking them against apps.json would reject every one of them.
            # Imported here rather than at the top for the reason capabilities.py does the same:
            # the desktop adapter pulls in the guest transport, and a native or hub run should
            # not pay for it on every import of the pipeline.
            from .world.desktop_app import desktop_apps, validate_desktop_apps

            validate_desktop_apps(apps, desktop_apps(root / "catalogs" / "desktop_apps.json"))
        else:
            catalog_apps = read(root / "catalogs" / "apps.json")["apps"]
            known_apps = {app["id"] for app in catalog_apps}
            if unknown_apps := set(apps) - known_apps:
                raise ValueError(
                    f"available_runtime_apps contains unknown catalog app IDs: {sorted(unknown_apps)}"
                )
            if runtime_adapter(config) == "hub" and (not_hub := set(apps) - hub_apps(catalog_apps).keys()):
                raise ValueError(
                    "hub runtime_adapter requires hub apps with schema documents; "
                    f"not seedable through the hub contract: {sorted(not_hub)}"
                )
    else:
        runtime_adapter(config)
    settings = config["generation"]
    batch_limit = settings.get("workflow_batch_size")
    if batch_limit is not None and (type(batch_limit) is not int or batch_limit < 1):
        raise ValueError("workflow_batch_size must be a positive integer")
    retries = settings.get("max_model_retries", 0)
    if type(retries) is not int or retries < 0:
        raise ValueError("max_model_retries must be a nonnegative integer")
    c = company_target if company_target is not None else settings["companies"]
    t = task_target if task_target is not None else settings["tasks"]
    if c < 1 or t < c:
        raise ValueError("need positive companies and at least one task per company")
    if workers is not None:
        settings["workers"] = workers
    if settings["workers"] < 1:
        raise ValueError("workers must be positive")
    if seed is not None:
        settings["seed"] = seed
    config["diversity"] = dict(config["diversity"], **read(root / "catalogs" / "embedding.json"))
    prompts, skills = freeze_skills(root)
    policy = config["models"].get("review_policy", "different_model")
    if policy not in ("different_model", "fresh_session"):
        raise ValueError(f"unknown review policy: {policy}")
    run_id = now().replace(":", "").replace(".", "-") + "-" + uuid.uuid4().hex[:6]
    directory = root / "runs" / run_id
    directory.mkdir(parents=True)
    shutil.copytree(root / "catalogs", directory / "catalogs")
    state = {
        "schema_version": "1",
        "id": run_id,
        "created_at": now(),
        "status": "ready",
        "config": config,
        "targets": {"companies": c, "tasks": t},
        "target_history": [],
        "prompts": prompts,
        "skills": skills,
        "catalog_hash": digest(read(directory / "catalogs" / "manifest.json")),
        "jobs": {},
        "entries": [],
        "companies": {},
        "discovery_receipts": [],
        "error": "",
    }
    provenance = Catalogs(directory / "catalogs").reference_provenance()
    write(directory / "reference-provenance.json", provenance)
    state["reference_provenance"] = {"path": "reference-provenance.json", "hash": digest(provenance)}
    if config["diversity"].get("cross_run", False):
        state["portfolio"] = freeze_history(root, directory)
    write(directory / "run.json", state)
    return directory


def prepare(root, run_dir, state, job):
    """Research and expand; returns paths, never mutates the shared run manifest."""
    directory = run_dir / "jobs" / job["id"]
    directory.mkdir(parents=True, exist_ok=True)
    models, catalogs, sources = (
        Models(state["config"], run_dir),
        Catalogs(run_dir / "catalogs"),
        Sources(root),
    )
    candidate = research_job.AppCandidate.model_validate(job["candidate"])
    feedback = job.get("feedback", "")
    version = state["config"].get("design", {}).get("workflow_version", "1")
    worker_floor = minimum_workers(state["config"])
    version_args = {"workflow_version": version} if version != "1" else {}
    company_path, authors_path = directory / "company.json", directory / "authors.json"
    rewrite_company = job.get("rewrite_company", False)
    if job.get("reuse_dossier") and not company_path.exists():
        base = run_dir / "jobs" / job["reuse_dossier"]
        write(company_path, read(base / "company.json"))
        write(authors_path, {**read(base / "authors.json"), "receipts": author_receipts(base)})
    if not company_path.exists() or rewrite_company:
        app_pages = research_job.capture_app_evidence(
            sources, candidate, catalogs, state["config"].get("design", {}).get("available_runtime_apps")
        )
        previous = (
            Company.model_validate(read(company_path)) if rewrite_company and company_path.exists() else None
        )
        needs_sources = False
        for attempt in range(state["config"]["generation"]["max_revisions"] + 1):
            evidence = []
            try:
                company, receipt = research_job.research(
                    models,
                    catalogs,
                    state["prompts"]["research"],
                    candidate,
                    job["coverage"],
                    feedback,
                    previous=previous,
                    excerpts=[
                        *(sources.excerpts(previous) if isinstance(previous, Company) else []),
                        *app_pages,
                    ],
                    needs_sources=needs_sources,
                )
            except ModelOutputInvalid as exc:
                record_author(directory, exc.receipt)
                previous = exc.raw
                feedback = json.dumps({"errors": str(exc)})
                write(
                    directory / f"research-issues-{job.get('revisions', 0)}-{attempt}.json",
                    {"errors": feedback},
                )
                if attempt == state["config"]["generation"]["max_revisions"]:
                    raise DesignRejected(feedback) from exc
                continue
            record_author(directory, receipt)
            write(
                directory / f"research-attempt-{job.get('revisions', 0)}-{attempt}.json",
                {"company": company.model_dump(), "receipt": receipt},
            )
            try:
                if (
                    company.id != candidate.id
                    or company.real_firm != candidate.real_firm
                    or company.sector != candidate.sector
                ):
                    raise ValueError("dossier must preserve the supplied candidate id, real_firm and sector")
                catalogs.validate(company)
                evidence = sources.evidence(company)
                errors = evidence_errors(company, evidence)
                if errors:
                    raise ValueError("; ".join(errors))
                write(company_path, company.model_dump())
                break
            except ValueError as exc:
                excerpts = sources.excerpts(company)
                previous = company
                # A missing page needs research even when other pages were captured.
                # A bad quote on an available page only needs a local correction.
                captured = {e["url"] for e in excerpts if e["excerpt"].strip()}
                needs_sources = not any(e["kind"] == "sourced" for e in evidence) or any(
                    e["kind"] == "sourced" and e["status"] != "supported" and e["source_url"] not in captured
                    for e in evidence
                )
                feedback = json.dumps(
                    {
                        "errors": str(exc),
                        "source_status": evidence,
                        "instruction": "Repair only the identified issues. Use exact text from the captured excerpts for quotes; remove unsupported factual detail or explicitly label a justified inference. Do not replace one unsupported quote with another paraphrase.",
                    }
                )
                write(
                    directory / f"research-issues-{job.get('revisions', 0)}-{attempt}.json",
                    {"errors": feedback},
                )
                if attempt == state["config"]["generation"]["max_revisions"]:
                    raise DesignRejected(feedback) from exc
    company = Company.model_validate(read(company_path))
    catalogs.validate(company)
    evidence = sources.evidence(company)
    if errors := evidence_errors(company, evidence):
        raise DesignRejected("; ".join(errors))
    write(directory / "evidence.json", evidence)
    selected = job.get("outline_ids") or [o.id for o in company.outlines[: job["expansion_count"]]]
    workflows_path = directory / "workflows.json"
    preserved = []
    if workflows_path.exists() and job.get("feedback"):
        verdict = json.loads(job["feedback"])
        previous_raw, verdict = apply_brief_fixes(read(workflows_path), verdict)
        previous = [Workflow.model_validate(w) for w in previous_raw]
        repair_ids = {t["workflow_id"] for t in verdict["tasks"] if t["verdict"] == "revise"}
        # A dossier-only correction does not require reauthoring accepted work.
        # Broken references do; every retained design still gets a fresh review
        # bound to the corrected dossier, so changed meaning is not auto-accepted.
        for workflow in previous:
            try:
                validate_workflow(company, workflow, version=version, minimum_workers=worker_floor)
            except ValueError:
                repair_ids.add(workflow.id)
        selected = [w.outline_id for w in previous if w.id in repair_ids]
        preserved = [w for w in previous if w.id not in repair_ids]
        feedback = json.dumps(
            {
                "review": verdict,
                "previous_workflows": [w.model_dump() for w in previous if w.id in repair_ids],
                "instruction": "Revise only the requested workflows. Keep their IDs; remove decorative workers unless the existing task genuinely needs their work.",
            }
        )
        if not selected:
            write(workflows_path, [w.model_dump() for w in preserved])
            return job["id"]
    if workflows_path.exists() and not job.get("feedback"):
        workflows = [Workflow.model_validate(w) for w in read(workflows_path)]
    else:
        for attempt in range(state["config"]["generation"]["max_revisions"] + 1):
            try:
                design_settings = state["config"].get("design", {})
                legacy_agency = design_settings.get("agency", False)
                tools = design_settings.get("read_only_tools", legacy_agency)
                selection = design_settings.get("select_outlines", legacy_agency)
                amendment = design_settings.get("allow_amendment", legacy_agency)
                if tools or selection or amendment or design_settings.get("feature_matrix", False):
                    fixed = (
                        selected if not selection or job.get("outline_ids") or job.get("feedback") else None
                    )
                    company, workflows, receipt, decision = workflow_job.design(
                        models,
                        state["prompts"]["expand"],
                        company,
                        evidence,
                        catalogs,
                        sources.excerpts(company),
                        len(selected),
                        selected=fixed,
                        coverage=job["coverage"],
                        feedback=feedback,
                        allow_amendment=amendment and not job.get("reuse_dossier") and not preserved,
                        read_only_tools=tools,
                        reserved_cells=[
                            w.model_dump()["feature_cell"]
                            for w in preserved
                            if w.model_dump().get("feature_cell")
                        ],
                        **version_args,
                    )
                    write(company_path, company.model_dump())
                    write(directory / f"design-decision-{job.get('revisions', 0)}-{attempt}.json", decision)
                else:
                    workflows, receipt = workflow_job.expand(
                        models,
                        state["prompts"]["expand"],
                        company,
                        evidence,
                        selected,
                        feedback,
                        **version_args,
                    )
                record_author(directory, receipt)
                write(
                    directory / f"expansion-attempt-{job.get('revisions', 0)}-{attempt}.json",
                    {"workflows": [w.model_dump() for w in workflows], "receipt": receipt},
                )
                break
            except ValueError as exc:
                if isinstance(exc, ModelOutputInvalid):
                    record_author(directory, exc.receipt)
                feedback = json.dumps(
                    {
                        "instruction": "Repair the structural validation error, preserving the substantive design.",
                        "errors": str(exc),
                        "previous_output": getattr(exc, "raw", None),
                    }
                )
                write(
                    directory / f"expansion-issues-{job.get('revisions', 0)}-{attempt}.json",
                    {"errors": feedback},
                )
                if attempt == state["config"]["generation"]["max_revisions"]:
                    raise DesignRejected(feedback) from exc
        write(workflows_path, [w.model_dump() for w in preserved + workflows])
    for workflow in preserved + workflows:
        validate_workflow(company, workflow, version=version, minimum_workers=worker_floor)
    return job["id"]


def load_companies(root, state):
    return {cid: Company.model_validate(read(root / path)) for cid, path in state["companies"].items()}


def chosen(root, state, catalogs):
    return select(
        state["entries"],
        load_companies(root, state),
        state["targets"]["companies"],
        state["targets"]["tasks"],
        catalogs.sectors,
        state["config"]["generation"]["seed"],
    )


def coverage(root, state, catalogs):
    entries = chosen(root, state, catalogs)
    companies = load_companies(root, state)
    accepted_ids = {e["company_id"] for e in state["entries"] if e["verdict"] == "accept"}
    surface = state["config"].get("design", {}).get("available_runtime_apps")
    run_dir = root / "runs" / state["id"]
    historical_companies, historical_assignments = history_app_context(run_dir, state)
    assignments = [*historical_assignments, *assigned_app_history(state.get("jobs", {}), run_dir)]
    steering = {
        "companies": [*historical_companies, *(companies[cid] for cid in sorted(accepted_ids))],
        "surface": catalogs.apps if surface is None else surface,
        "sectors": [s["name"] for s in catalogs.sectors],
    }
    ids = {e["company_id"] for e in entries}
    designs = [read(root / e["workflow_path"]) for e in entries]
    socs = {
        worker.soc
        for design in designs
        for worker in companies[design["company_id"]].workers
        if worker.id in design["worker_ids"]
    }
    result = {
        **company_histograms(**steering),
        **app_coverage(root, state, catalogs),
        "assigned_app_history": assignments,
        "in_flight_app_usage": in_flight_app_usage(state.get("jobs", {}), accepted_ids, run_dir),
        "companies": len(ids),
        "tasks": len(entries),
        "sectors": dict(Counter(companies[cid].sector for cid in ids)),
        "uncovered_priority_occupations": {
            s: v["title"] for s, v in catalogs.anchors.items() if s not in socs
        },
        "existing_work": [{"title": w["title"], "decision_problem": w["decision_problem"]} for w in designs],
    }
    result["target_apps"] = target_apps(result["uncovered_apps"], state.get("discovery_app_requests", {}))
    result["under_used_app_categories"] = under_used_app_categories(
        catalogs, result, surface, state["config"].get("design", {}).get("standard_apps", [])
    )
    matrix_enabled = state["config"].get("design", {}).get("feature_matrix", False)
    if "portfolio" in state or matrix_enabled:
        history = load_history(root / "runs" / state["id"], state)
        # Matrix planning covers every accepted task, including tasks outside the
        # current selection quota. Rejected and merely drafted cells do not count.
        accepted = (
            [read(root / e["workflow_path"]) for e in state["entries"] if e["verdict"] == "accept"]
            if matrix_enabled
            else designs
        )
        portfolio = [*history, *(Workflow.model_validate(w) for w in accepted)]
        # Matrix planning needs the counters below even before any work exists;
        # a substantive summary needs a frozen corpus or accepted work to summarize.
        if "portfolio" in state or portfolio:
            result["portfolio"] = summarize(portfolio, include_cells=matrix_enabled)
        if matrix_enabled:
            result["cell_histogram"] = cell_histogram(portfolio)
            result["company_feature_cells"] = {
                cid: [
                    w.model_dump()["feature_cell"]
                    for w in portfolio
                    if w.company_id == cid and w.model_dump().get("feature_cell")
                ]
                for cid in {w.company_id for w in portfolio if w.feature_cell is not None}
            }
    return result


def review_snapshot(root, run_dir, state, job_id, encoder):
    """Freeze review inputs before dispatch; reuse the same neighbors after interruption."""
    directory = run_dir / "jobs" / job_id
    company_raw, workflows_raw = read(directory / "company.json"), read(directory / "workflows.json")
    company = Company.model_validate(company_raw)
    workflows = [Workflow.model_validate(w) for w in workflows_raw]
    for workflow in workflows:
        validate_workflow(
            company,
            workflow,
            version=state["config"].get("design", {}).get("workflow_version", "1"),
            minimum_workers=minimum_workers(state["config"]),
        )
    evidence = read(directory / "evidence.json")
    receipts = author_receipts(directory)
    authors = {r["model"] for r in receipts}
    policy = state["config"]["models"].get("review_policy", "different_model")
    inputs = {
        "company": company_raw,
        "workflows": workflows_raw,
        "evidence": evidence,
        "receipts": receipts,
        "config": state["config"],
        "prompt": state["prompts"]["review"],
        "source_excerpts": Sources(root).excerpts(company),
    }
    job = state["jobs"][job_id]
    if previous := job.get("review_snapshot"):
        snapshot = read(run_dir / previous)
        if digest(snapshot) != Path(previous).stem:
            raise ValueError("review snapshot changed")
        if snapshot["inputs"] == inputs:
            return run_dir / previous
    existing = load_history(run_dir, state) + [
        Workflow.model_validate(read(root / e["workflow_path"]))
        for e in state["entries"]
        if e["verdict"] == "accept" and e["workflow_id"] not in {w.id for w in workflows}
    ]
    comparisons = {
        w.id: nearest(w, existing + workflows, encoder, state["config"]["diversity"]["neighbors"])
        for w in workflows
    }
    expected = bound_review(
        company_raw, workflows_raw, evidence, state["prompts"]["review"], authors, policy, receipts
    )
    snapshot = {
        "inputs": inputs,
        "neighbors": comparisons,
        "input_hash": expected,
        "accepted": {
            e["workflow_id"]: e["workflow_path"] for e in state["entries"] if e["verdict"] == "accept"
        },
    }
    path = directory / "review-inputs" / f"{digest(snapshot)}.json"
    if not path.exists():
        write(path, snapshot)
    return path


def review_candidate(run_dir, snapshot_path):
    """Worker-only model work; no live manifest or mutable candidate inputs are consulted."""
    snapshot = read(snapshot_path)
    if digest(snapshot) != snapshot_path.stem:
        raise ValueError("review snapshot changed")
    inputs = snapshot["inputs"]
    config = inputs["config"]
    policy = config["models"].get("review_policy", "different_model")
    receipts = inputs["receipts"]
    authors = {r["model"] for r in receipts}
    directory = snapshot_path.parent.parent
    result_path = directory / "review-results" / snapshot_path.name
    review_path = result_path if result_path.exists() else directory / "review.json"
    cached = read(review_path) if review_path.exists() else {}
    if (
        cached.get("input_hash") == snapshot["input_hash"]
        and cached.get("neighbors") == snapshot["neighbors"]
        and cached.get("source_excerpts") == inputs["source_excerpts"]
        and cached.get("separate_witness", False)
        == config.get("design", {}).get("review_separate_witness", False)
    ):
        result = cached
    else:
        result = review(
            Models(config, run_dir),
            inputs["prompt"],
            Company.model_validate(inputs["company"]),
            [Workflow.model_validate(w) for w in inputs["workflows"]],
            inputs["evidence"],
            inputs["source_excerpts"],
            snapshot["neighbors"],
            authors,
            policy=policy,
            receipts=receipts,
            separate_witness=config.get("design", {}).get("review_separate_witness", False),
        )
    validate_separation(policy, result["receipt"], receipts)
    if not result_path.exists():
        write(result_path, result)
    return snapshot, result


def publish(root, run_dir, state, job_id, encoder, reviewed=None):
    """Coordinator-only admission; direct callers may still review synchronously."""
    if reviewed is None:
        reviewed = review_candidate(run_dir, review_snapshot(root, run_dir, state, job_id, encoder))
    snapshot, result = reviewed
    inputs = snapshot["inputs"]
    company_raw, workflows_raw = inputs["company"], inputs["workflows"]
    receipts = inputs["receipts"]
    company = Company.model_validate(company_raw)
    directory = run_dir / "jobs" / job_id
    if (
        read(directory / "company.json") != company_raw
        or read(directory / "workflows.json") != workflows_raw
        or read(directory / "evidence.json") != inputs["evidence"]
        or author_receipts(directory) != receipts
    ):
        raise ValueError("candidate changed during review")
    review_path = directory / "review.json"
    if review_path.exists() and (previous_review := read(review_path)) != result:
        write(directory / "review-history" / f"{digest(previous_review)}.json", previous_review)
    write(review_path, result)
    expected = snapshot["input_hash"]
    # Keep the independent verdict/receipt intact. Conflict feedback is coordinator policy.
    verdict = copy.deepcopy(result["review"])
    job = state["jobs"][job_id]
    if job.get("reuse_dossier") and verdict["company_verdict"] != "accept":
        job.update(
            status="rejected",
            reasons=[
                "Extra outlines cannot mutate a dossier with published workflows.",
                *verdict["company_reasons"],
            ],
        )
        return
    if verdict["company_verdict"] == "reject":
        job.update(status="rejected", reasons=verdict["company_reasons"])
        return
    fixed_workflows, verdict = apply_brief_fixes(workflows_raw, verdict)
    new_workflows = [
        Workflow.model_validate(read(root / e["workflow_path"]))
        for e in state["entries"]
        if e["verdict"] == "accept"
        and snapshot["accepted"].get(e["workflow_id"]) != e["workflow_path"]
        and e["workflow_id"] not in {w["id"] for w in workflows_raw}
    ]
    conflicts = []
    threshold = state["config"]["diversity"].get("admission_similarity", 0.9)
    for task in verdict["tasks"]:
        if task["verdict"] != "accept" or task["novelty"] != "distinct" or not new_workflows:
            continue
        workflow = Workflow.model_validate(next(w for w in workflows_raw if w["id"] == task["workflow_id"]))
        neighbors = nearest(workflow, new_workflows, encoder, state["config"]["diversity"]["neighbors"])
        seen = {n["workflow_id"]: digest(n["workflow"]) for n in snapshot["neighbors"][workflow.id]}
        close = [
            n
            for n in neighbors
            if n["similarity"] >= threshold and seen.get(n["workflow_id"]) != digest(n["workflow"])
        ]
        if close:
            conflicts.append({"workflow_id": workflow.id, "neighbors": close, "threshold": threshold})
            task["verdict"] = "revise"
            task["reasons"].append(
                "Admission conflict: newly accepted near-duplicates were absent from the review snapshot. "
                "Revise the substantive decision/dependencies to distinguish this work: "
                + json.dumps(close, ensure_ascii=False)
            )
    if conflicts:
        conflict = {"snapshot_hash": digest(snapshot), "conflicts": conflicts}
        history = job.setdefault("admission_conflicts", [])
        if conflict not in history:
            history.append(conflict)
    revision_requested = verdict["company_verdict"] == "revise" or any(
        t["verdict"] == "revise" for t in verdict["tasks"]
    )
    revision_basis = digest({"company": company_raw, "workflows": workflows_raw, "authors": receipts})
    charged = revision_basis in job.get("revision_bases", [])
    if revision_requested and (
        charged or job.get("revisions", 0) < state["config"]["generation"]["max_revisions"]
    ):
        feedback = copy.deepcopy(verdict)
        for task in feedback["tasks"]:
            if task["verdict"] == "accept" and task.get("brief_fix"):
                task["verdict"] = "modify_brief"
        job.update(
            status="queued",
            revisions=job.get("revisions", 0) + (0 if charged else 1),
            revision_bases=list(dict.fromkeys([*job.get("revision_bases", []), revision_basis])),
            feedback=json.dumps(feedback),
            rewrite_company=verdict["company_verdict"] == "revise",
        )
        return
    if verdict["company_verdict"] != "accept":
        job.update(status="rejected", reasons=verdict["company_reasons"])
        return
    # At the repair limit retain unaffected tasks, as for ordinary review revisions.
    # Conflicted tasks cannot be published with a fabricated independent verdict.
    conflicted_ids = {c["workflow_id"] for c in conflicts}
    company_path = Path("data") / "companies" / company.id / f"{digest(company_raw)}.json"
    write(root / company_path, company_raw)
    state["companies"][company.id] = str(company_path)
    family_lookup = {e["workflow_id"]: e["family_id"] for e in state["entries"]}
    local_links = {t["workflow_id"]: t["duplicate_of"] for t in verdict["tasks"] if t["novelty"] == "variant"}
    for task in verdict["tasks"]:
        wid = task["workflow_id"]
        if wid in conflicted_ids:
            continue
        family = wid
        while family in local_links:
            family = local_links[family]
        family = family_lookup.get(family, family)
        workflow_raw = next(w for w in fixed_workflows if w["id"] == wid)
        path = Path("data") / "workflows" / wid / f"{digest(workflow_raw)}.json"
        write(root / path, workflow_raw)
        entry = {
            **task,
            "company_id": company.id,
            "family_id": family,
            "workflow_path": str(path),
            "company_path": str(company_path),
            "review_path": str(review_path.relative_to(root)),
            "review_input_hash": expected,
            "job_id": job_id,
        }
        state["entries"] = [e for e in state["entries"] if e["workflow_id"] != wid] + [entry]
    job.update(status="reviewed", feedback="", rewrite_company=False)


def workflow_batch_size(state, count):
    """Bound output per job; absent in historical configs, preserving their behavior."""
    return min(count, state["config"]["generation"].get("workflow_batch_size", count))


def enqueue_extra(root, run_dir, state, catalogs):
    for cid, company in load_companies(root, state).items():
        related = [j for j in state["jobs"].values() if j["candidate"]["id"] == cid]
        if any(j["status"] in ("queued", "running", "drafted", "pending_model") for j in related):
            continue
        used = set()
        for job in related:
            path = run_dir / "jobs" / job["id"] / "workflows.json"
            if path.exists():
                used.update(w["outline_id"] for w in read(path))
        remaining = [o.id for o in company.outlines if o.id not in used]
        if not remaining:
            continue
        remaining = remaining[: workflow_batch_size(state, len(remaining))]
        base = next(j for j in related if not j.get("reuse_dossier"))
        job_id = f"{cid}--extra-{len(related)}"
        state["jobs"][job_id] = {
            "id": job_id,
            "candidate": base["candidate"],
            "status": "queued",
            "coverage": coverage(root, state, catalogs),
            "reuse_dossier": base["id"],
            "outline_ids": remaining,
            "expansion_count": len(remaining),
        }
        return True
    return False


def accepted_task_titles(run_dir, limit=200):
    """Titles of tasks already accepted in this run, so the next company is asked for different work."""
    try:
        rows = json.loads((run_dir / "dataset.json").read_text()).get("workflows", [])
    except (OSError, ValueError, AttributeError):
        return []
    titles = []
    for row in rows[-limit:]:
        try:
            titles.append(json.loads((Path(row["workflow_path"])).read_text())["title"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return titles


def fill_queue(root, run_dir, state, catalogs):
    cov = coverage(root, state, catalogs)
    target = state["targets"]
    if cov["companies"] >= target["companies"] and enqueue_extra(root, run_dir, state, catalogs):
        return True
    limit = target["companies"] * state["config"]["generation"]["candidate_multiplier"]
    originals = [j for j in state["jobs"].values() if not j.get("reuse_dossier")]
    if len(originals) >= limit:
        return False
    planned = Counter(cov["sectors"])
    for j in originals:
        if j["status"] in ("queued", "running", "drafted"):
            planned[j["candidate"]["sector"]] += 1
    targets = sector_targets(catalogs.sectors, target["companies"])
    cov["sector_deficits"] = {s: max(0, targets[s] - planned[s]) for s in sorted(targets)}
    exclusions = state.get("excluded_companies", [])
    existing = list(
        dict.fromkeys([j["candidate"]["real_firm"] for j in originals] + [c["real_firm"] for c in exclusions])
    )
    generation = state["config"]["generation"]
    remaining = limit - len(originals)
    # Discover every sector with a deficit at once, largest deficit first, so early cohorts span
    # sectors instead of saturating the two biggest quotas before the others start.
    deficits = {s: targets[s] - planned[s] for s in targets if targets[s] - planned[s] > 0}
    if not deficits:
        deficits = {min(targets, key=lambda s: (-(targets[s] - planned[s]), s)): 1}
    parallel = max(1, min(len(deficits), generation.get("discovery_parallel", generation.get("workers", 1))))
    sectors = sorted(deficits, key=lambda s: (-deficits[s], s))[:parallel]
    counts = {s: min(generation["discovery_batch"], deficits[s]) for s in sectors}
    while sum(counts.values()) > remaining:
        biggest = max(counts, key=lambda s: (counts[s], s))
        if counts[biggest] <= 1:
            counts.pop(biggest)
            continue
        counts[biggest] -= 1
    sectors = [s for s in sectors if counts.get(s)]
    retries = generation.get("max_model_retries", 0) if generation.get("isolate_model_failures") else 0
    failures = []
    requests = state.setdefault("discovery_app_requests", {})
    discovery_coverage = {}
    for sector in sectors:
        targets_for_call = target_apps(cov.get("uncovered_apps", []), requests)
        discovery_coverage[sector] = {**cov, "target_apps": targets_for_call}
        for app in targets_for_call:
            requests[app] = requests.get(app, 0) + 1
    # Reserve each parallel call's targets before dispatch so retries and resumes
    # do not continually start at the beginning of the catalog.
    write(run_dir / "run.json", state)

    def discover_sector(sector):
        for attempt in range(retries + 1):
            try:
                found, receipt = research_job.discover(
                    Models(state["config"], run_dir),
                    catalogs,
                    state["prompts"]["discover"],
                    sector,
                    list(existing),
                    discovery_coverage[sector],
                    counts[sector],
                )
                return sector, found, receipt, None
            except ModelUnavailable as exc:
                retry = exc.retryable and not exc.global_failure and attempt < retries
                failures.append({"at": now(), "sector": sector, "error": str(exc), "scheduled_retry": retry})
                if not retry:
                    return sector, None, None, exc
        return sector, None, None, None

    with ThreadPoolExecutor(max_workers=len(sectors)) as pool:
        results = list(pool.map(discover_sector, sectors))
    if failures:
        state.setdefault("discovery_failure_history", []).extend(failures)
        write(run_dir / "run.json", state)
    fatal = [exc for _, _, _, exc in results if exc is not None]
    if fatal and all(found is None for _, found, _, _ in results):
        raise fatal[0]
    for _, found, receipt, exc in results:
        if receipt is not None:
            state["discovery_receipts"].append(receipt)
    added = 0
    queues = {sector: list(found.candidates) for sector, found, _, exc in results if found is not None}
    while queues and added < remaining:
        for sector in list(queues):
            if added >= remaining:
                break
            if not queues[sector]:
                queues.pop(sector)
                continue
            candidate = queues[sector].pop(0)
            receipt = next(r for s_, _, r, _ in results if s_ == sector)
            prior = exclusions + [
                j["candidate"] for j in state["jobs"].values() if not j.get("reuse_dossier")
            ]
            if matches := overlap(candidate.model_dump(), prior):
                state.setdefault("excluded_discoveries", []).append(
                    {"candidate": candidate.model_dump(), "matches": matches, "receipt": receipt}
                )
                continue
            if (
                candidate.sector != sector
                or candidate.id in state["jobs"]
                or candidate.real_firm.casefold() in {x.casefold() for x in existing}
            ):
                continue
            candidate_coverage = research_coverage(root, state, catalogs, candidate)
            state["jobs"][candidate.id] = {
                "id": candidate.id,
                "candidate": candidate.model_dump(),
                "status": "queued",
                "coverage": candidate_coverage,
                "expansion_count": workflow_batch_size(
                    state, math.ceil(target["tasks"] / target["companies"]) + 1
                ),
            }
            existing.append(candidate.real_firm)
            added += 1
    return bool(added)


def unreadable_hosts(root, minimum=2):
    """Hosts the local capture cache has only ever failed on, so research can skip them.

    Capture failures are the reason 70 of 82 recorded research repairs happened, and
    34 hosts account for 114 attempts that never once produced text. The author cannot
    learn this from its own web search; the cache already knows it for every company.
    """
    captured, failed = Counter(), Counter()
    for path in sorted((Path(root) / "data" / "sources").glob("*.json")):
        try:
            page = read(path)
        except (OSError, ValueError):
            continue
        host = urlparse(page.get("url", "")).netloc.lower()
        if not host:
            continue
        if page.get("status") == "captured" and not page.get("error") and (page.get("text") or "").strip():
            captured[host] += 1
        else:
            failed[host] += 1
    return sorted(host for host, count in failed.items() if count >= minimum and not captured[host])


def research_coverage(root, state, catalogs, candidate):
    """Coordinator-owned frozen payload; earlier queue entries reserve their apps."""
    cov = coverage(root, state, catalogs)
    cov["unreadable_hosts"] = unreadable_hosts(root)
    design = state["config"].get("design", {})
    if design.get("available_runtime_apps") is not None:
        cov["assigned_apps"] = assign_apps(
            catalogs,
            candidate,
            cov,
            seed=state["config"]["generation"]["seed"],
            surface=design["available_runtime_apps"],
            standard_apps=design.get("standard_apps", []),
        )
    return cov


def validate_run_inputs(run_dir, state):
    """Check frozen ordinary-run inputs at resume and report boundaries."""
    if "campaign_hash" in state or (run_dir / "campaign.json").exists():
        raise ValueError("validation campaigns are archived; use the provenance-wave1 archive reader")
    if "portfolio" in state:
        load_history(run_dir, state)
    if "reference_provenance" in state:
        manifest = state["reference_provenance"]
        path = (run_dir / manifest["path"]).resolve()
        if not path.is_relative_to(run_dir.resolve()):
            raise ValueError("reference provenance path escapes run directory")
        if digest(read(path)) != manifest["hash"]:
            raise ValueError("frozen reference provenance changed")


def run(root, run_dir, companies=None, tasks=None, workers=None):
    from .report import export

    with run_lock(run_dir):
        reset_cancellation()
        state = read(run_dir / "run.json")
        validate_run_inputs(run_dir, state)
        old = dict(state["targets"])
        new = {
            "companies": companies if companies is not None else old["companies"],
            "tasks": tasks if tasks is not None else old["tasks"],
        }
        if (
            new["companies"] < old["companies"]
            or new["tasks"] < old["tasks"]
            or new["tasks"] < new["companies"]
        ):
            raise ValueError("resume may extend targets, never shrink them")
        if new != old:
            state["target_history"].append({"at": now(), "from": old, "to": new})
            state["targets"] = new
        width = workers if workers is not None else state["config"]["generation"]["workers"]
        if width < 1:
            raise ValueError("workers must be positive")
        state.setdefault("invocations", []).append(
            {
                "started_at": now(),
                "workers": width,
                "targets": dict(new),
                "model_adapter_version": MODEL_ADAPTER_VERSION,
            }
        )
        for job in state["jobs"].values():
            if job["status"] in ("running", "pending_model", "error"):
                job["status"] = "drafted" if job.get("retry_stage") == "review" else "queued"
        state.update(status="running", error="")
        catalogs = Catalogs(run_dir / "catalogs")
        encoder = Embeddings(
            root, state["config"]["diversity"]["model"], state["config"]["diversity"]["revision"]
        )
        active, stop, exhausted = {}, False, False
        write(run_dir / "run.json", state)
        print(
            f"run {state['id']}: target {new['companies']} companies / {new['tasks']} workflows", flush=True
        )
        try:
            with WorkerPool(max_workers=width) as pool:
                while True:
                    cov = coverage(root, state, catalogs)
                    reached = cov["companies"] == new["companies"] and cov["tasks"] == new["tasks"]
                    while len(active) < width and not stop and not reached:
                        active_ids = {jid for jid, stage in active.values()}
                        queued = next(
                            (
                                j
                                for j in state["jobs"].values()
                                if j["status"] in ("queued", "drafted") and j["id"] not in active_ids
                            ),
                            None,
                        )
                        if queued is None:
                            if exhausted:
                                break
                            # Account for active candidates before scheduling more company research.
                            if cov["companies"] + len(active) >= new["companies"]:
                                break
                            if not fill_queue(root, run_dir, state, catalogs):
                                # Discovery exhaustion must still drain active authors through
                                # review and any revisions they need before this invocation ends.
                                exhausted = True
                                break
                            queued = next(j for j in state["jobs"].values() if j["status"] == "queued")
                        drafted = queued["status"] == "drafted"
                        queued["retry_stage"] = "review" if drafted else "prepare"
                        try:
                            if drafted:
                                snapshot_path = review_snapshot(root, run_dir, state, queued["id"], encoder)
                                queued["review_snapshot"] = str(snapshot_path.relative_to(run_dir))
                                # An in-flight review stays drafted on disk. Persist its exact
                                # inputs before the model call so restart uses the same cache key.
                                write(run_dir / "run.json", state)
                                future = pool.submit(review_candidate, run_dir, snapshot_path)
                            else:
                                if (
                                    queued.get("candidate")
                                    and "assigned_apps" not in queued.get("coverage", {})
                                    and not queued.get("reuse_dossier")
                                    and state["config"].get("design", {}).get("available_runtime_apps")
                                    is not None
                                ):
                                    queued["coverage"] = research_coverage(
                                        root,
                                        state,
                                        catalogs,
                                        research_job.AppCandidate.model_validate(queued["candidate"]),
                                    )
                                queued.setdefault("coverage", {})["accepted_task_titles"] = (
                                    accepted_task_titles(run_dir)
                                )
                                queued.update(status="running")
                                queued.pop("review_snapshot", None)
                                write(run_dir / "run.json", state)
                                future = pool.submit(
                                    prepare, root, run_dir, copy.deepcopy(state), copy.deepcopy(queued)
                                )
                            active[future] = (queued["id"], queued["retry_stage"])
                        except Exception as exc:  # noqa: BLE001 -- checkpoint dispatch errors
                            stop = job_failure(state, queued["id"], exc) or stop
                            write(run_dir / "run.json", state)
                        print(
                            f"  {'review' if drafted else 'research/expand'} {queued['id']}",
                            flush=True,
                        )
                    if not active:
                        if reached or stop or exhausted:
                            break
                        if cov["companies"] >= new["companies"]:
                            if enqueue_extra(root, run_dir, state, catalogs):
                                continue
                            state["error"] = "available outlines exhausted before the task target"
                            break
                        if not fill_queue(root, run_dir, state, catalogs):
                            break
                        continue
                    done, _ = wait(active, timeout=30, return_when=FIRST_COMPLETED)
                    if not done:
                        print(
                            f"  waiting: {len(active)} jobs; selected {cov['companies']} companies / {cov['tasks']} workflows",
                            flush=True,
                        )
                    for future in sorted(done, key=lambda f: active[f]):
                        jid, stage = active.pop(future)
                        try:
                            result = future.result()
                            if stage == "prepare":
                                state["jobs"][jid].update(status="drafted", retry_stage="review")
                            else:
                                publish(root, run_dir, state, jid, encoder, reviewed=result)
                        except Exception as exc:  # noqa: BLE001 -- checkpoint errors before stopping
                            stop = job_failure(state, jid, exc) or stop
                        print(f"  {jid}: {state['jobs'][jid]['status']}", flush=True)
                        write(run_dir / "run.json", state)
                        if stage == "review" or state["jobs"][jid]["status"] != "drafted":
                            export(root, run_dir, state)
        except (ModelUnavailable, KeyboardInterrupt) as exc:
            state["error"] = str(exc) or "interrupted by user"
        finally:
            cov = coverage(root, state, catalogs)
            pending = sum(j["status"] == "pending_model" for j in state["jobs"].values())
            if pending and not state["error"] and cov["tasks"] < new["tasks"]:
                state["error"] = f"{pending} jobs remain unavailable after bounded model recovery"
            state["status"] = (
                "complete"
                if cov["companies"] == new["companies"] and cov["tasks"] == new["tasks"]
                else "incomplete"
            )
            state["updated_at"] = now()
            report = export(root, run_dir, state)
            state["status"] = report["status"]
            write(run_dir / "run.json", state)
        print(f"{state['status']}: {cov['companies']} companies / {cov['tasks']} workflows", flush=True)
        return state
