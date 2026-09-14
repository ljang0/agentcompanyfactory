"""Read-only, per-stage artifact audit. No model calls and no network access."""

from collections import Counter

from .catalogs import Catalogs
from .provenance import author_receipts, validate_separation
from .report import build_report, call_receipts
from .review import evidence_errors, validate_review
from .schemas import Candidate, Company, Review, Workflow, minimum_workers, validate_workflow
from .sources import normalize
from .storage import digest, read


def audit_run(root, run_dir, state=None, report=None):
    state = state or read(run_dir / "run.json")
    report = report or build_report(root, run_dir, state)
    catalogs = Catalogs(run_dir / "catalogs")
    stages = {
        name: {"checked": 0, "errors": {}, "quarantined": {}}
        for name in ("inputs", "discovery", "research", "expansion", "review", "selection")
    }

    def check(stage, key, function, quarantined=False):
        stages[stage]["checked"] += 1
        try:
            function()
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            stages[stage]["quarantined" if quarantined else "errors"][key] = str(exc)

    def inputs():
        if digest(read(run_dir / "catalogs" / "manifest.json")) != state["catalog_hash"]:
            raise ValueError("catalog manifest hash changed")
        if errors := catalogs.integrity_errors():
            raise ValueError(str(errors))
        for stage, skill in state.get("skills", {}).items():
            if digest(skill["source"]) != skill["hash"]:
                raise ValueError(f"frozen {stage} skill changed")
            if skill["source"].split("---", 2)[2].strip() + "\n" != state["prompts"][stage]:
                raise ValueError(f"{stage} prompt differs from frozen skill")

    check("inputs", "frozen_inputs", inputs)
    for job_id, job in state["jobs"].items():
        directory = run_dir / "jobs" / job_id
        check("discovery", job_id, lambda job=job: Candidate.model_validate(job["candidate"]))

        def research(directory=directory, job=job):
            company = Company.model_validate(read(directory / "company.json"))
            catalogs.validate(company)
            candidate = job["candidate"]
            if any(getattr(company, k) != candidate[k] for k in ("id", "real_firm", "sector")):
                raise ValueError("dossier changed candidate identity or sector")
            evidence = read(directory / "evidence.json")
            if errors := evidence_errors(company, evidence):
                raise ValueError("; ".join(errors))
            claims = {c.id: c for c in company.evidence}
            if {e["claim_id"] for e in evidence} != set(claims):
                raise ValueError("incomplete evidence manifest")
            for e in evidence:
                claim = claims[e["claim_id"]]
                if any(e[k] != getattr(claim, k) for k in ("kind", "source_url", "quote")):
                    raise ValueError("claim differs from evidence manifest")
                if claim.kind == "sourced":
                    page = read(root / "data" / "sources" / f"{digest(claim.source_url)}.json")
                    if digest(page["text"]) != e["text_hash"] or normalize(claim.quote) not in normalize(
                        page["text"]
                    ):
                        raise ValueError("source text or quote changed")

        def expansion(directory=directory):
            company = Company.model_validate(read(directory / "company.json"))
            workflows = [Workflow.model_validate(w) for w in read(directory / "workflows.json")]
            if len({w.id for w in workflows}) != len(workflows):
                raise ValueError("duplicate workflow identities")
            for workflow in workflows:
                validate_workflow(
                    company,
                    workflow,
                    version=state["config"].get("design", {}).get("workflow_version", "1"),
                    minimum_workers=minimum_workers(state["config"]),
                )

        def review(directory=directory):
            result = read(directory / "review.json")
            policy = state["config"]["models"].get("review_policy", "different_model")
            validate_separation(policy, result["receipt"], author_receipts(directory))
            workflows = [Workflow.model_validate(w) for w in read(directory / "workflows.json")]
            validate_review(Review.model_validate(result["review"]), workflows, result["neighbors"])

        for stage, filename, function in (
            ("research", "company.json", research),
            ("expansion", "workflows.json", expansion),
            ("review", "review.json", review),
        ):
            if (directory / filename).exists():
                # Expected failures in rejected, unpublished drafts are findings,
                # not reasons to repair or reject the accepted dataset again.
                published = any(e["job_id"] == job_id for e in state["entries"])
                check(stage, job_id, function, job["status"] == "rejected" and not published)
    stages["selection"]["checked"] = len(state["entries"])
    stages["selection"]["errors"] = report["validation_errors"]
    receipts = call_receipts(run_dir)
    for receipt in receipts:
        role = "research" if receipt["job"] == "research_repair" else receipt["job"]
        if receipt["model"] not in state["config"]["models"].get(role, []):
            stages["inputs"]["errors"][receipt["call_id"]] = "call used a model outside the frozen route"
    for stage in stages.values():
        stage["status"] = "fail" if stage["errors"] else ("pass" if stage["checked"] else "not_run")
    return {
        "run_id": state["id"],
        "scope": "Saved artifact contracts and provenance; not independent factual or difficulty validation.",
        "status": "fail"
        if any(s["errors"] for s in stages.values())
        else ("pass" if report["status"] == "complete" else "partial"),
        "stages": stages,
        "selected": report["counts"],
        "review_policy": report["review_policy"],
        "models_used": dict(Counter(r["model"] for r in receipts)),
        "jobs": dict(Counter(j["status"] for j in state["jobs"].values())),
        "limitations": [
            "Fresh-session same-model review has correlated blind spots.",
            "Economic sector weights and occupation priorities guide coverage, not a representative sample.",
            (
                "Semantic novelty uses neighbors from frozen accepted local history and this run, not proven global uniqueness."
                if "portfolio" in state
                else "Semantic novelty is judged against supplied neighbors within this run, not proven globally."
            ),
            "App mappings, worker permissions, material instantiation and graders are not executed here.",
            "Calendar days and estimated human hours do not measure agent horizon or multi-agent advantage.",
        ],
    }
