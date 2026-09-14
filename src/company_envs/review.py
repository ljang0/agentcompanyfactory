"""Evidence validation and a complete fresh-context verdict, bound to reviewed bytes."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

from .provenance import validate_separation
from .schemas import Review, Workflow
from .storage import bound_review, digest
from .workflows import check_plain_english


def evidence_errors(company, evidence):
    """Allow declared capture gaps without treating them as company grounding.

    New authors identify the core grounding explicitly. Legacy dossiers use
    operational evidence (excluding claims used only for software); entailment
    of the firm's identity, sector and operations remains a semantic review duty.
    """
    errors = [
        f"claim {e['claim_id']} is sourced but not supported by a captured page"
        for e in evidence
        if e["kind"] == "sourced" and e["status"] != "supported"
    ]
    errors.extend(
        f"claim {e['claim_id']} has a quote that does not match its captured page"
        for e in evidence
        if e["kind"] != "sourced"
        and e.get("capture_status") == "captured"
        and e.get("quote")
        and e["status"] != "supported"
    )
    software_ids = {eid for item in company.software for eid in item.evidence_ids}
    operation_ids = {eid for item in [*company.workers, *company.outlines] for eid in item.evidence_ids}
    core_ids = {c.id for c in company.evidence if c.id == "core-identity"} or {
        c.id for c in company.evidence if c.id not in software_ids or c.id in operation_ids
    }
    supported = {e["claim_id"] for e in evidence if e["kind"] == "sourced" and e["status"] == "supported"}
    if not core_ids & supported:
        errors.append(
            "the real firm's operations need captured source evidence: at least one core page must ground "
            "the real firm name, sector and operations summary; software-only evidence is insufficient"
        )
    inferred = sum(c.kind == "inferred" for c in company.evidence)
    if inferred > len(supported):
        errors.append(
            f"the dossier's grounding is mostly inferred ({inferred} inferred claims, "
            f"{len(supported)} captured sourced claims; synthetic choices excluded); capture more "
            "operating evidence or narrow unsupported factual detail"
        )
    for claim in company.evidence:
        if claim.kind != "sourced" and not claim.basis.strip():
            errors.append(f"claim {claim.id} needs an explicit inference/synthetic basis")
    return errors


def validate_review(review, workflows, neighbors):
    if review.company_verdict != "accept" and not review.company_reasons:
        raise ValueError("non-accept company verdict needs actionable reasons")
    ids = [t.workflow_id for t in review.tasks]
    if len(ids) != len(set(ids)) or set(ids) != {w.id for w in workflows}:
        raise ValueError("review must judge every workflow exactly once")
    allowed = {w.id for w in workflows} | {n["workflow_id"] for ns in neighbors.values() for n in ns}
    links = {}
    for task in review.tasks:
        # Older saved reviews have no severity. New brief fixes must supply one.
        allowed_verdicts = {
            "P0": {"reject"},
            "P1": {"modify_brief"},
            "P2": {"accept", "modify_brief"},
            "P3": {"accept"},
        }
        if task.severity is not None and task.verdict not in allowed_verdicts[task.severity]:
            raise ValueError(f"{task.severity} does not allow {task.verdict}")
        if task.verdict == "modify_brief":
            if task.severity not in {"P1", "P2"} or not task.brief_fix.strip():
                raise ValueError("modify_brief needs P1 or P2 severity and a nonempty brief_fix")
        elif task.brief_fix:
            raise ValueError("only modify_brief may supply brief_fix")
        if task.verdict in {"accept", "modify_brief"} and task.quality < 3:
            raise ValueError("accept requires usable quality (at least 3/5)")
        if task.verdict != "accept" and not task.reasons:
            raise ValueError("non-accept verdict needs actionable reasons")
        if task.novelty == "variant":
            if task.duplicate_of not in allowed or task.duplicate_of == task.workflow_id or not task.reasons:
                raise ValueError("variant needs a supplied comparison id and an explanation")
            links[task.workflow_id] = task.duplicate_of
        elif task.duplicate_of:
            raise ValueError("distinct workflow cannot name duplicate_of")
    for start in links:
        seen, cur = set(), start
        while cur in links:
            if cur in seen:
                raise ValueError("duplicate relationships cannot cycle")
            seen.add(cur)
            cur = links[cur]


def apply_brief_fixes(workflows, verdict):
    """Apply reviewed wording only; leave the original task and review evidence intact."""
    workflows, verdict = copy.deepcopy(workflows), copy.deepcopy(verdict)
    for task in verdict["tasks"]:
        if task["verdict"] != "modify_brief":
            continue
        workflow = next(w for w in workflows if w["id"] == task["workflow_id"])
        fixed = {**workflow, "brief": task["brief_fix"]}
        try:
            if not task["brief_fix"].strip():
                raise ValueError("brief_fix must not be blank")
            check_plain_english(Workflow.model_validate(fixed))
        except ValueError as exc:
            task["verdict"] = "revise"
            task["reasons"].append(f"Brief fix failed the plain-English check: {exc}")
        else:
            workflow["brief"] = task["brief_fix"]
            task["verdict"] = "accept"
    return workflows, verdict


def review_payload(company, workflows, evidence, excerpts, neighbors, *, separate_witness=False):
    """Stable presentation shared by generation and receipt verification.

    Separation is an ordering aid, not information withholding or an independent
    solve. Original workflow bytes remain the review's binding authority.
    """
    presented = copy.deepcopy(workflows)
    witnesses = {}
    if separate_witness:
        for workflow in presented:
            completion = workflow.get("completion")
            if completion is not None:
                witnesses[workflow["id"]] = completion.pop("feasible_path")
    payload = {
        "company": company,
        "workflows": presented,
        "evidence_status": evidence,
        "source_excerpts": excerpts,
        "nearby_workflows": neighbors,
    }
    if separate_witness:
        payload["review_presentation"] = {
            "order": "reconstruct_from_specification_then_check_author_witnesses",
            "blind": False,
            "scope": "Witnesses are separate evidence in this same call, not an independent solve.",
        }
        payload["author_witnesses"] = witnesses
    return payload


def review(
    models,
    prompt,
    company,
    workflows,
    evidence,
    excerpts,
    neighbors,
    authors,
    policy="different_model",
    receipts=None,
    *,
    separate_witness=False,
):
    originals = [w.model_dump() for w in workflows]
    payload = review_payload(
        company.model_dump(), originals, evidence, excerpts, neighbors, separate_witness=separate_witness
    )
    votes = max(1, int(getattr(models, "config", {}).get("generation", {}).get("review_votes", 1)))

    def one_vote(index):
        body = {**payload, **({"vote": index} if votes > 1 else {})}
        verdict, receipt = models.call(
            "review",
            prompt + "\n" + json.dumps(body, ensure_ascii=False),
            Review,
            avoid=authors if policy == "different_model" else (),
            validate=lambda result: validate_review(result, workflows, neighbors),
        )
        validate_review(verdict, workflows, neighbors)
        validate_separation(policy, receipt, receipts or [{"model": model} for model in authors])
        return verdict, receipt

    if votes == 1:
        verdict, receipt = one_vote(0)
    else:
        with ThreadPoolExecutor(max_workers=votes) as pool:
            ballots = list(pool.map(one_vote, range(votes)))
        verdict = merge_votes([v for v, _ in ballots])
        validate_review(verdict, workflows, neighbors)
        # Keep the first call's provenance for existing receipt readers. Every
        # ballot below retains its own full verdict and actual call receipt.
        receipt = {
            **ballots[0][1],
            "votes": votes,
            "accepts": sum(v.company_verdict == "accept" for v, _ in ballots),
            "ballots": [{"verdict": v.model_dump(), "receipt": r} for v, r in ballots],
        }
    return {
        "review": verdict.model_dump(),
        "receipt": receipt,
        "input_hash": bound_review(
            payload["company"], originals, evidence, prompt, authors, policy, receipts
        ),
        "review_policy": policy,
        "separate_witness": separate_witness,
        "authors": sorted(authors),
        "neighbors": neighbors,
        "source_excerpts": excerpts,
    }


def merge_votes(ballots):
    """Vote on the company and each task; retain distinct reasons for repairs."""

    def decision(values):
        if sum(v in {"accept", "modify_brief"} for v in values) * 2 > len(values):
            return "accept"
        return "reject" if values.count("reject") * 2 > len(values) else "revise"

    def reasons(groups):
        seen, merged = set(), []
        for group in groups:
            for issue in group:
                if issue[:80] not in seen:
                    seen.add(issue[:80])
                    merged.append(issue)
        return merged

    company_verdict = decision([v.company_verdict for v in ballots])
    tasks = []
    for task in ballots[0].tasks:
        readings = [next(t for t in v.tasks if t.workflow_id == task.workflow_id) for v in ballots]
        outcome = decision([t.verdict for t in readings])
        if outcome == "accept":
            # Keep a required fix from the accepting side, in ballot order.
            chosen = next(
                (t for t in readings if t.verdict == "modify_brief"),
                next(t for t in readings if t.verdict in {"accept", "modify_brief"}),
            ).model_copy(deep=True)
        else:
            chosen = next((t for t in readings if t.verdict == outcome), readings[0]).model_copy(deep=True)
            chosen.verdict = outcome
            chosen.brief_fix = ""
            # A split vote requests author review; it is not a critic severity.
            if outcome == "revise":
                chosen.severity = None
            chosen.reasons = reasons(
                t.reasons for t in readings if t.verdict not in {"accept", "modify_brief"}
            )
        tasks.append(chosen)
    return Review(
        company_verdict=company_verdict,
        company_reasons=(
            next(v.company_reasons for v in ballots if v.company_verdict == "accept")
            if company_verdict == "accept"
            else reasons(v.company_reasons for v in ballots if v.company_verdict != "accept")
        ),
        tasks=tasks,
    )


def validate_review_receipt(result, payload, prompt, votes, workflows, policy, authors):
    """Verify saved ballots against their packets and recompute the majority."""
    votes = max(1, int(votes))
    receipt = result["receipt"]
    if receipt.get("votes", 1) != votes:
        raise ValueError("review vote count differs from frozen configuration")
    ballots = receipt.get("ballots", []) if votes > 1 else [{"verdict": result["review"], "receipt": receipt}]
    if len(ballots) != votes:
        raise ValueError("review receipt must retain every ballot")
    readings = []
    for index, ballot in enumerate(ballots):
        call = ballot["receipt"]
        validate_separation(policy, call, authors)
        body = {**payload, **({"vote": index} if votes > 1 else {})}
        expected = digest(prompt + "\n" + json.dumps(body, ensure_ascii=False))
        if call.get("status") != "complete" or call.get("prompt_hash") != expected:
            raise ValueError("review prompt, source excerpts or comparisons changed")
        verdict = Review.model_validate(ballot["verdict"])
        validate_review(verdict, workflows, result["neighbors"])
        readings.append(verdict)
    if votes > 1:
        if receipt.get("accepts") != sum(v.company_verdict == "accept" for v in readings):
            raise ValueError("review accept count differs from saved ballots")
        if merge_votes(readings).model_dump() != result["review"]:
            raise ValueError("review verdict differs from saved ballots")
