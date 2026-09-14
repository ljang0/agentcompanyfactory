"""Local, fail-closed export of reviewed designs. This never calls a model."""

from collections import Counter
from pathlib import Path

from .app_affinity import app_coverage
from .catalogs import Catalogs
from .diversity import select
from .pipeline import validate_run_inputs
from .portfolio import assigned_app_history, cell_histogram, company_histograms
from .provenance import author_receipts, validate_separation
from .review import (
    apply_brief_fixes,
    evidence_errors,
    review_payload,
    validate_review,
    validate_review_receipt,
)
from .schemas import Company, Review, Workflow, minimum_workers, validate_workflow
from .sources import Sources, normalize
from .storage import bound_review, digest, read, run_lock, write
from .workflows import difficulty_distribution, infer_difficulty


def _labeled_design(root, entry):
    """Use a backfilled label only while its reviewed source is unchanged."""
    design = read(Path(root) / entry["workflow_path"])
    if not design.get("difficulty") and entry.get("difficulty_input_hash") == digest(design):
        design = {**design, "difficulty": entry.get("difficulty")}
    return design


def corpus_difficulty(root, current=()):
    """Count each workflow ID once; later sorted runs and current entries take precedence."""
    designs = {}
    for path in sorted((Path(root) / "runs").glob("*/dataset.json")):
        for entry in read(path).get("workflows", []):
            if entry.get("verdict") == "accept":
                designs[entry["workflow_id"]] = _labeled_design(root, entry)
    for entry in current:
        if entry.get("verdict") == "accept":
            designs[entry["workflow_id"]] = _labeled_design(root, entry)
    return difficulty_distribution(designs.values())


def label_difficulty(root, run_id):
    """Backfill dataset labels without changing hashed designs or review evidence."""
    root = Path(root)
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run_id must name one run")
    directory = root / "runs" / run_id
    if not (directory / "dataset.json").is_file():
        raise FileNotFoundError(directory / "dataset.json")
    with run_lock(directory):
        dataset = read(directory / "dataset.json")
        for entry in dataset["workflows"]:
            if entry.get("verdict") != "accept":
                continue
            design = read(root / entry["workflow_path"])
            entry.update(
                difficulty=design.get("difficulty") or infer_difficulty(design),
                difficulty_source="authored" if design.get("difficulty") else "heuristic-v1",
                difficulty_input_hash=digest(design),
            )
        config = read(directory / "run.json")["config"] if (directory / "run.json").exists() else {}
        result = difficulty_distribution(
            (
                _labeled_design(root, entry)
                for entry in dataset["workflows"]
                if entry.get("verdict") == "accept"
            ),
            config.get("generation", {}).get("difficulty_mix"),
        )
        dataset.setdefault("coverage", {})["difficulty"] = result
        dataset["coverage"]["corpus_difficulty"] = corpus_difficulty(root, dataset["workflows"])
        write(directory / "dataset.json", dataset)
    return result


def call_receipts(run_dir):
    ledger = run_dir / "receipts.json"
    records = read(ledger) if ledger.exists() else []
    records += [read(p) for p in sorted((run_dir / "calls").glob("*/attempt-*/receipt.json"))]
    return list({(r["call_id"], r["attempt"]): r for r in records}.values())


def verified_entries(root, run_dir, state):
    valid, errors, checked = [], {}, {}
    try:
        validate_run_inputs(run_dir, state)
    except (OSError, ValueError, KeyError) as exc:
        return [], {"inputs": str(exc)}
    catalogs = Catalogs(run_dir / "catalogs")
    if (
        digest(read(run_dir / "catalogs" / "manifest.json")) != state["catalog_hash"]
        or catalogs.integrity_errors()
    ):
        return [], {"catalogs": "frozen reference catalog changed"}
    for entry in state["entries"]:
        try:
            key = entry["review_path"]
            if key not in checked:
                result = read(root / key)
                directory = (root / key).parent
                company_raw = read(directory / "company.json")
                workflows_raw = read(directory / "workflows.json")
                evidence = read(directory / "evidence.json")
                receipts = author_receipts(directory)
                authors = {r["model"] for r in receipts}
                policy = state["config"]["models"].get("review_policy", "different_model")
                expected = bound_review(
                    company_raw,
                    workflows_raw,
                    evidence,
                    state["prompts"]["review"],
                    authors,
                    policy,
                    receipts,
                )
                validate_separation(policy, result["receipt"], receipts)
                if result["input_hash"] != expected:
                    raise ValueError("stale review or incomplete author lineage")
                if result.get("review_policy", "different_model") != policy:
                    raise ValueError("review policy differs from frozen configuration")
                if result["receipt"]["status"] != "complete" or set(result["authors"]) != authors:
                    raise ValueError("missing review provenance")
                company = Company.model_validate(company_raw)
                workflows = [Workflow.model_validate(w) for w in workflows_raw]
                catalogs.validate(company)
                for workflow in workflows:
                    validate_workflow(
                        company,
                        workflow,
                        version=state["config"].get("design", {}).get("workflow_version", "1"),
                        minimum_workers=minimum_workers(state["config"]),
                    )
                if problems := evidence_errors(company, evidence):
                    raise ValueError("; ".join(problems))
                claims = {c.id: c for c in company.evidence}
                if {e["claim_id"] for e in evidence} != set(claims):
                    raise ValueError("incomplete evidence manifest")
                for e in evidence:
                    claim = claims[e["claim_id"]]
                    if (
                        e["kind"] != claim.kind
                        or e["source_url"] != claim.source_url
                        or e["quote"] != claim.quote
                    ):
                        raise ValueError("evidence manifest differs from dossier claim")
                    if e["kind"] != "sourced":
                        continue
                    page = read(root / "data" / "sources" / f"{digest(claim.source_url)}.json")
                    if digest(page["text"]) != e["text_hash"] or normalize(claim.quote) not in normalize(
                        page["text"]
                    ):
                        raise ValueError("captured evidence changed")
                verdict = Review.model_validate(result["review"])
                separate_witness = state["config"].get("design", {}).get("review_separate_witness", False)
                if result.get("separate_witness", False) != separate_witness:
                    raise ValueError("review presentation differs from frozen configuration")
                payload = review_payload(
                    company_raw,
                    workflows_raw,
                    evidence,
                    result.get("source_excerpts", Sources(root).excerpts(company, legacy=True)),
                    result["neighbors"],
                    separate_witness=separate_witness,
                )
                validate_review_receipt(
                    result,
                    payload,
                    state["prompts"]["review"],
                    state["config"]["generation"].get("review_votes", 1),
                    workflows,
                    policy,
                    receipts,
                )
                validate_review(verdict, workflows, result["neighbors"])
                if verdict.company_verdict != "accept":
                    raise ValueError("company has not been accepted")
                fixed_workflows, effective = apply_brief_fixes(workflows_raw, verdict.model_dump())
                checked[key] = (
                    company_raw,
                    {w["id"]: w for w in fixed_workflows},
                    {t["workflow_id"]: t for t in effective["tasks"]},
                    expected,
                )
            company_raw, workflows_raw, verdicts, expected = checked[key]
            workflow_raw = workflows_raw[entry["workflow_id"]]
            for path, value in ((entry["company_path"], company_raw), (entry["workflow_path"], workflow_raw)):
                if Path(path).stem != digest(value) or read(root / path) != value:
                    raise ValueError("published artifact does not match reviewed content")
            if entry["review_input_hash"] != expected:
                raise ValueError("entry is bound to a different review")
            if entry["company_id"] != company_raw["id"]:
                raise ValueError("entry belongs to a different company")
            if any(entry[k] != v for k, v in verdicts[entry["workflow_id"]].items()):
                raise ValueError("entry verdict differs from independent review")
            if entry["novelty"] == "distinct" and entry["family_id"] != entry["workflow_id"]:
                raise ValueError("distinct workflow has an inconsistent family id")
            valid.append(entry)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors[entry["workflow_id"]] = str(exc)
    return valid, errors


def build_report(root, run_dir, state=None):
    root, run_dir = Path(root), Path(run_dir)
    state = state or read(run_dir / "run.json")
    entries, errors = verified_entries(root, run_dir, state)
    companies = {e["company_id"]: Company.model_validate(read(root / e["company_path"])) for e in entries}
    catalogs = Catalogs(run_dir / "catalogs")
    selected = select(
        entries,
        companies,
        state["targets"]["companies"],
        state["targets"]["tasks"],
        catalogs.sectors,
        state["config"]["generation"]["seed"],
    )
    ids = sorted({e["company_id"] for e in selected})
    inventory_socs = {w.soc for cid in ids for w in companies[cid].workers}
    saved = {
        e["workflow_id"]: e
        for e in (
            read(run_dir / "dataset.json").get("workflows", []) if (run_dir / "dataset.json").exists() else []
        )
    }
    selected = [dict(e) for e in selected]
    for entry in selected:
        previous = saved.get(entry["workflow_id"], {})
        raw = read(root / entry["workflow_path"])
        if raw.get("difficulty"):
            entry.update(difficulty=raw["difficulty"], difficulty_source="authored")
        elif previous.get("difficulty_input_hash") == digest(raw):
            entry.update(
                {key: previous[key] for key in ("difficulty", "difficulty_source", "difficulty_input_hash")}
            )
    designs = [_labeled_design(root, e) for e in selected]
    socs = {
        worker.soc
        for design in designs
        for worker in companies[design["company_id"]].workers
        if worker.id in design["worker_ids"]
    }
    receipts = call_receipts(run_dir)
    usage = Counter()
    for receipt in receipts:
        usage.update({k: v for k, v in receipt.get("usage", {}).items() if isinstance(v, (int, float))})
    complete = len(ids) == state["targets"]["companies"] and len(selected) == state["targets"]["tasks"]
    return {
        "schema_version": "1",
        "run_id": state["id"],
        "stage": "researched_designs_not_executable_environments",
        "status": "complete" if complete else "incomplete",
        "targets": state["targets"],
        "counts": {
            "companies": len(ids),
            "workflows": len(selected),
            "reviewed_candidates": len(entries),
            "variants": sum(e["novelty"] == "variant" for e in entries),
        },
        "coverage": {
            "difficulty": difficulty_distribution(
                designs, state["config"]["generation"].get("difficulty_mix")
            ),
            "corpus_difficulty": corpus_difficulty(root, selected),
            **app_coverage(root, state, catalogs),
            **company_histograms(
                (companies[cid] for cid in {e["company_id"] for e in entries if e["verdict"] == "accept"}),
                surface=state["config"].get("design", {}).get("available_runtime_apps", catalogs.apps),
                sectors=[s["name"] for s in catalogs.sectors],
            ),
            "assigned_app_history": assigned_app_history(state.get("jobs", {}), run_dir),
            **(
                {"cell_histogram": cell_histogram(designs)}
                if state["config"].get("design", {}).get("feature_matrix", False)
                else {}
            ),
            "sectors": dict(Counter(companies[cid].sector for cid in ids)),
            "priority_occupations": sorted(socs & catalogs.anchors.keys()),
            "company_inventory_priority_occupations": sorted(inventory_socs & catalogs.anchors.keys()),
            "uncovered_priority_occupations": sorted(catalogs.anchors.keys() - socs),
            "workers_per_company": {cid: len(companies[cid].workers) for cid in ids},
            "teams_per_company": {cid: len(companies[cid].teams) for cid in ids},
            "structure_distributions": {
                "teams_per_company": dict(sorted(Counter(len(companies[cid].teams) for cid in ids).items())),
                "workers_per_company": dict(
                    sorted(Counter(len(companies[cid].workers) for cid in ids).items())
                ),
                "teams_per_workflow": dict(sorted(Counter(len(w["team_ids"]) for w in designs).items())),
                "workers_per_workflow": dict(sorted(Counter(len(w["worker_ids"]) for w in designs).items())),
            },
        },
        "generation": {
            "calls": len(receipts),
            "failed_calls": sum(r["status"] not in ("complete", "running") for r in receipts),
            "in_flight_calls": sum(r["status"] == "running" for r in receipts),
            "recovered_receipts": sum(bool(r.get("recovery_note")) for r in receipts),
            "known_cost_usd": sum(r["cost_usd"] for r in receipts if r.get("cost_usd") is not None),
            "calls_without_cost": sum(r.get("cost_usd") is None for r in receipts),
            "model_seconds": sum(r.get("seconds", 0) for r in receipts),
            "usage": dict(usage),
            "job_model_failures": sum(j.get("model_failures", 0) for j in state["jobs"].values()),
            "scheduled_model_retries": sum(
                e.get("scheduled_retry", False)
                for j in state["jobs"].values()
                for e in j.get("model_failure_history", [])
            )
            + sum(e.get("scheduled_retry", False) for e in state.get("discovery_failure_history", [])),
        },
        "novelty_scope": {
            "comparison_pool": "frozen accepted local history plus current run"
            if "portfolio" in state
            else "current run only",
            "history": state.get("portfolio"),
            "neighbors_per_workflow": state["config"]["diversity"]["neighbors"],
            "limitation": "Retrieved comparisons and model judgments, not corpus-wide uniqueness or calibrated retrieval recall.",
        },
        "reference_provenance": state.get("reference_provenance"),
        "validation_errors": errors,
        "run_error": state.get("error", ""),
        "catalog_hash": state["catalog_hash"],
        "embedding": state["config"]["diversity"],
        "review_policy": state["config"]["models"].get("review_policy", "different_model"),
        "implementation_history": state.get("implementation_history", []),
        "invocations": state.get("invocations", []),
        "companies": {
            cid: next(e["company_path"] for e in selected if e["company_id"] == cid) for cid in ids
        },
        "workflows": selected,
        "split_rule": "Keep a family_id in one evaluation split; consider holding out entire companies too.",
    }


def export(root, run_dir, state=None):
    from .audit import audit_run
    from .schemas import WorkflowBatch
    from .workflows import generation_schema

    report = build_report(root, run_dir, state)
    write(run_dir / "AUDIT.json", audit_run(Path(root), Path(run_dir), state, report))
    write(run_dir / "receipts.json", call_receipts(run_dir))
    write(run_dir / "dataset.json", report)
    write(
        run_dir / "assignments.json",
        {
            entry["workflow_id"]: Workflow.model_validate(
                read(Path(root) / entry["workflow_path"])
            ).assignment()
            for entry in report["workflows"]
        },
    )
    write(run_dir / "schemas" / "company.schema.json", Company.model_json_schema())
    config = (state or read(run_dir / "run.json"))["config"]
    schema = generation_schema(
        WorkflowBatch, config.get("design", {}).get("workflow_version", "1"), minimum_workers(config)
    )
    # Exported files can contain new labels and older unlabeled tasks together.
    schema["$defs"]["Workflow"]["properties"]["difficulty"] = Workflow.model_json_schema()["properties"][
        "difficulty"
    ]
    write(
        run_dir / "schemas" / "workflow.schema.json",
        {**schema["$defs"]["Workflow"], "$defs": schema["$defs"]},
    )
    lines = [
        f"# Stage 1 run {report['run_id']}",
        "",
        f"Status: {report['status']}. These are designs, not runnable environments.",
        "",
        f"Selected: {report['counts']['companies']} companies / {report['counts']['workflows']} workflows.",
        "",
        "| Company | Teams | Workers | Workflows |",
        "| --- | ---: | ---: | ---: |",
    ]
    for cid, path in report["companies"].items():
        co = read(Path(root) / path)
        entries = [e for e in report["workflows"] if e["company_id"] == cid]
        lines.append(
            f"| {co['name']} (analogue: {co['real_firm']}) | {len(co['teams'])} | {len(co['workers'])} | {len(entries)} |"
        )
    lines += [
        "",
        "App coverage counts real firms across accepted dossiers in data/companies and this run's jobs.",
        "Run counts include reservations and may overlap accepted firms; they do not prove adoption.",
        "",
        "Uncovered apps: " + (", ".join(report["coverage"]["uncovered_apps"]) or "none"),
        "",
        "| App | Accepted firms across runs | Firms in this run's jobs |",
        "| --- | ---: | ---: |",
        *[
            f"| {app} | {counts['accepted_firms']} | {counts['run_firms']} |"
            for app, counts in report["coverage"]["app_coverage"].items()
        ],
        "",
        "Costs with no provider-reported price are unknown, not zero.",
        "",
        f"Validation errors: {len(report['validation_errors'])}. See dataset.json for evidence, review and artifact paths.",
    ]
    for title, key in (("Run difficulty", "difficulty"), ("Corpus difficulty", "corpus_difficulty")):
        distribution = report["coverage"][key]
        lines += [
            "",
            f"## {title}",
            "",
            "Shares use labeled tasks only; missing labels are counted separately.",
            "",
            "| Difficulty | Tasks | Share | Target | Difference (percentage points) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for level, count in distribution["counts"].items():
            share = distribution["shares"].get(level)
            target = distribution["target"].get(level)
            delta = distribution["difference_percentage_points"].get(level)
            lines.append(
                f"| {level} | {count} | "
                + " | ".join(
                    [
                        f"{share:.1%}" if share is not None else "not yet",
                        f"{target:.1%}" if target is not None else "—",
                        f"{delta:+.1f}" if delta is not None else "—",
                    ]
                )
                + " |"
            )
    (run_dir / "REPORT.md").write_text("\n".join(lines) + "\n")
    return report
