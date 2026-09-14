"""Task/assessment co-design against immutable worker views; no claim of live necessity."""

from pathlib import Path

from company_envs.schemas import TaskAssessment
from company_envs.storage import digest, read

from .hub_identity import visible_to
from .world_check import primary_ids

TASK_RULES = """This accepted-world contract overrides legacy shared-view or world-retirement rules.
The world is accepted and immutable. Workers use distinct identities with the supplied app grants,
mailboxes, private chats and file permissions. Never broaden access or assign new authority.
An unsuitable task can be rejected or replaced without retiring or reseeding this company.
Write assessment together with the workflow. It is private: never expose it or the feasible path
in the public assignment, records or worker desktops. Cover every success criterion exactly once
with a positive weight, must-pass flag, exact supporting input references, a substantive rubric
and concrete failure cases. Every participant, including the manager, needs one dependency naming
visible input references, the work they produce, other workers who consume it, and why losing that
contribution changes the business result. Cite existing authority records where relevant.
Do not use ceremonial approvals, arbitrary record edits, or a count of workers as proof of need.
Name legitimate alternative successes and shortcuts that must fail. Criteria must reflect the
public goal or discoverable policies. This design will still need independent calibration and real
worker/input ablation trials; structural validation alone does not prove collaboration."""


def indexed_records(collection, value):
    """Expose native primary IDs, including Slack IDs and records keyed by map keys."""
    if isinstance(value, list):
        for item in value:
            yield from indexed_records(collection, item)
    elif isinstance(value, dict):
        ids = primary_ids(collection, value)
        if ids:
            for rid in sorted(ids):
                yield str(rid), value
        else:
            for key, item in value.items():
                if isinstance(item, dict) and not primary_ids(collection, item):
                    yield str(key), item
                else:
                    yield from indexed_records(collection, item)


def validate_assessment(task, snapshot):
    from .task_author import REFERENCE

    if task.assessment is None:
        raise ValueError("Accepted-world tasks require a co-authored assessment")
    assessment = task.assessment
    if [c.criterion for c in sorted(assessment.criteria, key=lambda c: c.criterion)] != list(
        range(1, len(task.success_criteria) + 1)
    ):
        raise ValueError("Assessment must cover every success criterion exactly once")
    if not any(c.must_pass for c in assessment.criteria):
        raise ValueError("Assessment needs at least one must-pass business outcome")
    workers = set(task.worker_ids)
    dependencies = {d.worker_id: d for d in assessment.dependencies}
    if set(dependencies) != workers or len(dependencies) != len(assessment.dependencies):
        raise ValueError("Every participating worker needs exactly one dependency")
    grants, identities = snapshot["worker_apps"], snapshot["identities"]
    contributions = {c.worker_id: c for c in task.contributions}
    from .state_seed import canonical_people

    people = canonical_people(snapshot.get("world", {}), list(workers))
    views = {}
    for worker in workers:
        if worker not in grants or worker not in identities or worker not in contributions:
            raise ValueError(f"Missing frozen worker access: {worker}")
        if not contributions[worker].apps or set(contributions[worker].apps) - set(grants[worker]):
            raise ValueError(f"Task must name existing app grants without widening them: {worker}")
        views[worker] = {}
        for app in grants[worker]:
            if app not in snapshot["states"]:
                continue
            person = identities[worker].get(app)
            if person is None and snapshot.get("identity_keys", {}).get(app):
                raise ValueError(f"Missing app identity: {worker}/{app}")
            # Apps without a signed-in-user collection use the canonical person,
            # exactly as load_world does when building their worker proxies.
            views[worker][app] = visible_to(snapshot["states"][app], person or people.get(worker))

    def resolve(reference, states):
        match = REFERENCE.fullmatch(reference)
        if not match:
            raise ValueError(f"Assessment requires an exact native record reference: {reference}")
        app, collection, rid = match.group("app", "collection", "id")
        hits = [r for i, r in indexed_records(collection, states.get(app, {}).get(collection)) if i == rid]
        if len(hits) != 1:
            raise ValueError(f"Assessment record is missing, ambiguous or inaccessible: {reference}")

    for criterion in assessment.criteria:
        for reference in criterion.evidence:
            resolve(reference, snapshot["states"])
    for worker, dependency in dependencies.items():
        if worker in dependency.consumed_by or set(dependency.consumed_by) - workers:
            raise ValueError(f"Dependency must be consumed by other participating workers: {worker}")
        for reference in [*dependency.inputs, *dependency.authority_sources]:
            resolve(reference, views[worker])
    return {
        "criteria": len(assessment.criteria),
        "workers": len(workers),
        "necessity": "requires semantic review and team ablations",
    }


def require_task_world(root, folder, task_id):
    """Reject stale task lineage before paying for reference or verifier authoring."""
    folder = Path(folder)
    if not (folder / "world/CORE.json").exists() and not (folder / "world/POPULATION.json").exists():
        return None
    from company_envs.schemas import Workflow

    from .task_author import seeded_snapshot

    snapshot = seeded_snapshot(folder, root=root)
    task = folder / "tasks" / task_id
    if read(task / "WORLD.json")["frozen_hash"] != digest(read(folder / "world/FROZEN.json")):
        raise ValueError("Task belongs to a different frozen world")
    workflow = Workflow.model_validate(read(task / "workflow.json"))
    assessment = TaskAssessment.model_validate(read(task / "assessment.json"))
    if workflow.assessment != assessment:
        raise ValueError("Saved assessment differs from the accepted task")
    validate_assessment(workflow, snapshot)
    return assessment.model_dump()


def assessment_sources(assessment, states, extra_references=()):
    """Keep the exact unchanged policy and input records available to authors and judges."""
    from .task_author import REFERENCE

    references = {ref for criterion in assessment["criteria"] for ref in criterion["evidence"]}
    references.update(extra_references)
    references.update(
        ref
        for dependency in assessment["dependencies"]
        for ref in [*dependency["inputs"], *dependency["authority_sources"]]
    )
    result = {}
    for reference in sorted(references):
        match = REFERENCE.fullmatch(reference)
        if match is None:
            raise ValueError(f"Invalid assessment source: {reference}")
        app, collection, rid = match.group("app", "collection", "id")
        records = [
            record
            for key, record in indexed_records(collection, states.get(app, {}).get(collection))
            if key == rid
        ]
        if len(records) != 1:
            raise ValueError(f"Missing or ambiguous assessment source: {reference}")
        result[reference] = records[0]
    return result
