"""Cheap design expansion over an existing dossier."""

import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from itertools import combinations
from pathlib import Path

from pydantic import Field

from .models import ModelOutputInvalid, strict_schema
from .portfolio import cell_key
from .schemas import (
    STANDARD_APPS,
    Company,
    Record,
    Software,
    Team,
    Worker,
    WorkflowBatch,
    minimum_workers,
    unique,
    validate_workflow,
)
from .schemas import (
    Workflow as BaseWorkflow,
)
from .stage_tools import definitions
from .storage import digest
from .world.capabilities import runtime_adapter, runtime_mounting, validate_runtime_mounting
from .world.hub_app import UI_STATE_KEYS, record_collections, top_level_keys

DECISION_TYPES = (
    "reconcile-discrepancy",
    "allocate-scarce-resource",
    "approve-or-decline",
    "prioritize-backlog",
    "investigate-root-cause",
    "negotiate-terms",
    "plan-schedule",
    "comply-with-policy",
    "correct-records",
    "respond-to-customer",
)

DIFFICULTY_MIX = {"easy": 30, "medium": 40, "hard": 30}


def difficulty_shares(value=None):
    """Read nonnegative weights for all three levels and return shares."""
    value = DIFFICULTY_MIX if value is None else value
    if (
        not isinstance(value, dict)
        or set(value) != set(DIFFICULTY_MIX)
        or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in value.values())
        or not 0 < sum(value.values()) < math.inf
    ):
        raise ValueError(
            "generation.difficulty_mix needs easy, medium and hard weights with a positive total"
        )
    return {level: value[level] / sum(value.values()) for level in DIFFICULTY_MIX}


def difficulty_distribution(workflows, mix=None):
    """Show missing labels separately; compare labeled tasks with the requested mix."""
    target = difficulty_shares(mix)
    counts = Counter(w.get("difficulty") or "unlabeled" for w in workflows)
    if counts.keys() - {*target, "unlabeled"}:
        raise ValueError("difficulty must be easy, medium or hard")
    labeled = sum(counts[level] for level in target)
    shares = {level: counts[level] / labeled if labeled else None for level in target}
    delta = {level: 100 * (shares[level] - target[level]) if labeled else None for level in target}
    return {
        "counts": {level: counts[level] for level in (*target, "unlabeled")},
        "total": sum(counts.values()),
        "labeled": labeled,
        "shares": shares,
        "target": target,
        "difference_percentage_points": delta,
        "distance_percentage_points": sum(abs(v) for v in delta.values()) / 2 if labeled else None,
    }


def infer_difficulty(workflow):
    """Estimate old labels from roster, active hours and the recorded decision type.

    Eight minimum hours is our proxy for sustained work. The decision types below
    suggest conflicting inputs, but cannot prove cross-app work or worker necessity.
    Routine work totaling less than an hour can be easy even with a larger roster.
    Missing evidence falls into medium; these labels still need rollout calibration.
    """
    workers = len(set(workflow.get("worker_ids", [])))
    effort = workflow.get("estimated_human_effort", {})
    decision = (workflow.get("feature_cell") or {}).get("decision_type")
    if (
        workers
        and 0 < effort.get("maximum_hours", math.inf) < 1
        and decision in {"correct-records", "respond-to-customer"}
    ):
        return "easy"
    if (
        workers >= 3
        and effort.get("minimum_hours", 0) >= 8
        and decision
        in {"reconcile-discrepancy", "allocate-scarce-resource", "investigate-root-cause", "negotiate-terms"}
    ):
        return "hard"
    return "medium"


# Rosters answer "who", never "which one should we do". They are legitimate supporting collections
# and eleven accepted cells use one, so this only ranks them last among equals. Kept separate from
# amplify.IDENTITY_COLLECTIONS on purpose: that set gates edit authorization, this one ranks cells.
ROSTER_COLLECTIONS = frozenset(
    {"users", "user", "currentUser", "members", "people", "workers", "accounts", "account"}
)


def decision_surface_rank(counts, collections):
    """Rank a bundle by how well its thinnest side could carry a decision. Lower is better.

    A decisive collection has to hold enough records that picking among them is the work. Mining
    against a frozen world had no such notion: with no portfolio histogram and a single matrix row,
    every tiebreaker was zero and the cell was drawn by digest, so designers were told to hang an
    offer-selection decision on google_drive_mock.users (5 rows), a scarce-resource allocation on
    shopify_admin_mock.blogPosts (4 rows) and a comply-with-policy task on conditionalFormats (1).
    They refused, correctly, and the driver recorded the world as unminable.

    The accepted corpus does build on thin collections (median thin side is 7 records), so this
    steers the choice and never vetoes a cell: every cell stays reachable. Returns a constant when
    no record counts are supplied, leaving the unseeded design path bit-for-bit unchanged.
    """
    if not counts:
        return 0
    return max(
        (0 if counts.get(c, 0) >= 20 else 1 if counts.get(c, 0) >= 5 else 2)
        + (1 if c.split(".", 1)[-1] in ROSTER_COLLECTIONS else 0)
        for c in collections
    )


class FeatureCell(Record):
    collections: list[str] = Field(min_length=1)
    decision_type: str


class Workflow(BaseWorkflow):
    """Design-only required field; the shared persistence contract remains optional."""

    feature_cell: FeatureCell


def company_collections(company, catalogs):
    """Read the pinned top-level contract, qualifying keys to avoid app collisions."""
    result = []
    for app_id in sorted({a for software in company.software for a in software.catalog_app_ids}):
        if app_id not in catalogs.apps:
            raise ValueError(f"feature matrix has unknown catalog app: {app_id}")
        path = catalogs.path / "app_schemas" / f"{app_id}.md"
        if not path.is_file():
            raise ValueError(f"feature matrix needs a top-level State Schema for {app_id}")
        text = path.read_text()
        # record_collections already refuses UI state; the fallback has to refuse it too. Twenty of
        # the 94 pinned schemas (salesforce_mock, ServiceNow_mock, jira_mock, airtable_mock, ...)
        # have no machine-readable record table and fall through to here, which offered cells like
        # ServiceNow_mock.shoppingCart and airtable_mock.ui as a task's decisive collections.
        # No published cell uses a UI_STATE_KEYS key, so nothing accepted depends on them.
        keys = record_collections(text) or [
            key for key in top_level_keys(text) or () if key not in UI_STATE_KEYS
        ]
        if not keys:
            raise ValueError(f"feature matrix needs a top-level State Schema for {app_id}")
        result.extend(f"{app_id}.{key}" for key in keys)
    if not result:
        raise ValueError("feature matrix needs company software with catalog app collections")
    return sorted(set(result))


def build_feature_matrix(
    company,
    catalogs,
    count,
    seed,
    coverage=None,
    *,
    selected=None,
    reserved=(),
    available_collections=None,
    difficulty_mix=None,
):
    """Assign cells before storytelling; use unused cells first, then least used.

    Two-collection bundles bound the search space while permitting cross-app work.
    Local collection/decision counts spread each batch across the available surface.
    Reserved cells belong to retained tasks at this company and may never be reused.

    available_collections may be a mapping of collection -> seeded record count, in which case
    denser collections are preferred once coverage and local spread have had their say.
    """
    target = difficulty_shares(difficulty_mix)
    collections = company_collections(company, catalogs)
    record_counts = available_collections if isinstance(available_collections, Mapping) else {}
    if available_collections is not None:
        collections = sorted(set(collections) & set(available_collections))
        if not collections:
            raise ValueError("feature matrix needs populated seeded collections")
    coverage = coverage or {}
    histogram = coverage.get("cell_histogram", coverage.get("portfolio", {}).get("cell_histogram", {}))
    company_cells = coverage.get("company_feature_cells", {}).get(company.id, [])
    blocked = {cell_key(cell) for cell in [*reserved, *company_cells]}
    candidates = []
    # A task's decisive work belongs in the company's own systems: when the company has a
    # domain app, every bundle carries at least one of its collections; the standard bundle
    # (mail, chat, calendar, drive, docs) supplies the second collection, not both.
    domain = [c for c in collections if c.split(".")[0] not in STANDARD_APPS]
    bundles = [
        b
        for b in combinations(collections, min(2, len(collections)))
        if not domain or any(c in domain for c in b)
    ]
    for bundle in bundles:
        for decision in DECISION_TYPES:
            cell = {"collections": list(bundle), "decision_type": decision}
            key = cell_key(cell)
            if key not in blocked:
                candidates.append((key, cell, digest([seed, company.id, cell])))
    if count < 1 or count > len(candidates):
        raise ValueError("feature matrix cannot supply the required number of distinct cells")
    quotas = {level: math.floor(count * share) for level, share in target.items()}
    remainder = sorted(
        target,
        key=lambda level: (-(count * target[level] - quotas[level]), digest([seed, company.id, level])),
    )
    for level in remainder[: count - sum(quotas.values())]:
        quotas[level] += 1
    levels = sorted(
        [(level, n) for level, total in quotas.items() for n in range(total)],
        key=lambda item: digest([seed, company.id, item]),
    )
    collection_counts, decision_counts = Counter(), Counter()
    rows = []
    for index in range(count):
        chosen = min(
            candidates,
            key=lambda item: (
                histogram.get(item[0], 0),
                sum(collection_counts[c] for c in item[1]["collections"]),
                decision_counts[item[1]["decision_type"]],
                decision_surface_rank(record_counts, item[1]["collections"]),
                item[2],
            ),
        )
        candidates.remove(chosen)
        cell = chosen[1]
        collection_counts.update(cell["collections"])
        decision_counts.update([cell["decision_type"]])
        row = {"task_index": index + 1, "feature_cell": cell, "difficulty": levels[index][0]}
        if selected is not None:
            row["outline_id"] = selected[index]
        rows.append(row)
    return rows


def validate_feature_cells(workflows, matrix, allowed):
    """Cells must be valid and distinct within the company; assigned rows steer, they do not veto.

    A model that deviates from its assigned row still produced a distinct, buildable kind of work;
    rejecting the whole company for that costs a research cycle and was the largest yield loss in
    the first digital pilot. Deviations are returned so coverage can record assigned vs chosen.
    """
    cells = []
    for workflow in workflows:
        cell = FeatureCell.model_validate(workflow.model_dump().get("feature_cell"))
        if len(set(cell.collections)) != len(cell.collections) or not set(cell.collections) <= set(allowed):
            raise ValueError("feature_cell collections must be distinct company app collections")
        if cell.decision_type not in DECISION_TYPES:
            raise ValueError("feature_cell decision_type is outside the fixed vocabulary")
        cells.append(cell_key(cell.model_dump()))
    if len(set(cells)) != len(cells):
        raise ValueError("duplicate feature_cell within company")
    assigned = {cell_key(row["feature_cell"]) for row in matrix}
    deviations = []
    for index, workflow in enumerate(workflows):
        row = next((r for r in matrix if r.get("outline_id") == workflow.outline_id), None)
        row = row or (matrix[index] if index < len(matrix) else {})
        if row.get("difficulty") != workflow.difficulty:
            deviations.append(
                {"workflow_id": workflow.id, "assigned": row.get("difficulty"), "chosen": workflow.difficulty}
            )
    return {
        "assigned": sorted(assigned),
        "chosen": cells,
        "deviations": sorted(set(cells) - assigned),
        "difficulty": {
            "assigned": difficulty_distribution(matrix)["counts"],
            "chosen": difficulty_distribution(w.model_dump() for w in workflows)["counts"],
            "deviations": deviations,
        },
    }


class DossierAmendment(Record):
    reason: str
    workers: list[Worker]
    teams: list[Team]
    software: list[Software]


class DesignBatch(WorkflowBatch):
    amendment: DossierAmendment | None
    selection_reason: str


class FeatureDesignBatch(DesignBatch):
    workflows: list[Workflow]


def tool_context(catalogs, excerpts):
    return catalogs.tool_context(excerpts)


def validate_execution_support(company, workflow, config):
    """A run's explicit construction scope, not a universal app-quality rule."""
    available = config.get("design", {}).get("available_runtime_apps")
    if available is None:
        return
    required = [app for app in company.software if app.id in workflow.software_requirement_ids]
    if any(not app.catalog_app_ids or not set(app.catalog_app_ids) <= set(available) for app in required):
        raise ValueError(
            "Task requires an app outside this run's declared runtime support; do not silently substitute"
        )
    validate_runtime_mounting(
        (app_id for app in required for app_id in app.catalog_app_ids), adapter=runtime_adapter(config)
    )


def generation_schema(
    batch_type,
    version,
    minimum_workers=2,
    execution_mode="scheduled",
    feature_matrix=False,
    difficulty_required=False,
):
    """Pin the output contract; don't silently upgrade frozen runs or permit downgrades."""
    if version not in ("1", "2"):
        raise ValueError(f"unknown workflow version: {version}")
    schema = strict_schema(batch_type)
    defs = schema["$defs"]
    fields = defs["Workflow"]["properties"]
    if difficulty_required:
        fields["difficulty"] = {"type": "string", "enum": list(DIFFICULTY_MIX)}
    else:
        fields.pop("difficulty")
        defs["Workflow"]["required"].remove("difficulty")
    if not feature_matrix and "feature_cell" in fields:
        fields.pop("feature_cell")
        defs["Workflow"]["required"].remove("feature_cell")
    if execution_mode not in {"scheduled", "digital"} or (execution_mode == "digital" and version != "2"):
        raise ValueError("Digital workflow generation requires V2")
    if execution_mode == "digital":
        fields["execution_mode"] = {"type": "string", "const": "digital"}
        fields["manager_id"] = {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,79}$"}
        fields["calendar_days"] = {"type": "null"}
        fields["events"]["maxItems"] = 0
        minimum_workers = max(3, minimum_workers)
    else:
        for name in ("execution_mode", "manager_id"):
            fields.pop(name)
            defs["Workflow"]["required"].remove(name)
        fields["calendar_days"] = {"type": "integer", "exclusiveMinimum": 0}
        fields["phases"]["minItems"] = 2
        defs["Outcome"]["properties"]["required_phase_ids"]["minItems"] = 1
        defs["WitnessStep"]["properties"]["phase_id"] = {
            "type": "string",
            "pattern": r"^[a-z][a-z0-9_-]{0,79}$",
        }
    defs["Workflow"]["properties"]["worker_ids"]["minItems"] = minimum_workers
    defs["Workflow"]["properties"]["schema_version"] = {"type": "string", "const": version}
    if version == "2":
        defs["Workflow"]["properties"]["completion"] = {"$ref": "#/$defs/CompletionContract"}
        defs["Phase"]["properties"]["when"]["minLength"] = 1
    else:
        for name, field in (("Workflow", "completion"), ("Phase", "when")):
            defs[name]["properties"].pop(field)
            defs[name]["required"].remove(field)
        # Remove unreachable V2 definitions from legacy requests.
        reachable = set()

        def visit(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    name = node["$ref"].rsplit("/", 1)[-1]
                    if name not in reachable:
                        reachable.add(name)
                        visit(defs[name])
                for key, value in node.items():
                    if key != "$defs":
                        visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(schema)
        schema["$defs"] = {key: value for key, value in defs.items() if key in reachable}
    return schema


def design(
    models,
    prompt,
    company,
    evidence,
    catalogs,
    excerpts,
    count,
    *,
    selected=None,
    coverage=None,
    feedback="",
    allow_amendment=True,
    read_only_tools=True,
    workflow_version="1",
    reserved_cells=(),
    seeded_world=None,
    seed_states=None,
):
    """Select/design with independently bounded read tools and roster amendments.

    A fixed selection may use tools; open selection does not authorize amendments.
    Legacy callers retain their former defaults; the coordinator freezes new flags.
    """
    eligible = selected if selected is not None else [o.id for o in company.outlines]
    count = min(count, len(eligible))
    if count < 1 or not set(eligible) <= {o.id for o in company.outlines}:
        raise ValueError("design needs a positive count and known outlines")
    payload = {
        "company": company.model_dump(),
        "evidence_status": evidence,
        # Filled in below from the tools the MCP server will actually expose. Naming a tool the
        # server never registers is worse than naming none: the designer looks for read_seed,
        # does not find it, and declines the whole batch.
        "available_tools": [],
        "eligible_outline_ids": eligible,
        "required_count": count,
        "fixed_selection": selected is not None,
        "portfolio_coverage": {k: v for k, v in (coverage or {}).items() if k != "accepted_task_titles"},
        "avoid_task_themes": (coverage or {}).get("accepted_task_titles", []),
        "allow_amendment": allow_amendment,
        "revision_feedback": feedback,
    }
    config = getattr(models, "config", {})
    difficulty_required = bool(config.get("generation", {}).get("difficulty_required", False))
    allowed_collections = None
    if seeded_world is not None:
        workflow_version = "2"
        # Keep the counts, not just the fact that a collection is nonempty: the matrix uses them to
        # avoid hanging a task's decisive work on a five-row roster when a real ledger is seeded.
        # ``record_counts`` is the shape-aware count when the caller supplies one; the app index
        # counts only dicts carrying an "id", which reads slack_mock.users (keyed by userId),
        # slack_mock.messages (a map of lists) and amazon_mock.wishlist (bare ids) as empty, so
        # 581 of the 3,695 collections in the 60 seeded worlds were withheld from the matrix --
        # every slack collection in every one of those worlds.
        allowed_collections = {
            collection: count
            for collection, count in (
                seeded_world.get("record_counts")
                or {
                    f"{app}.{key}": len(records)
                    for app, collections in seeded_world["app_index"].items()
                    for key, records in collections.items()
                }
            ).items()
            if count
        }
        payload["seeded_world"] = seeded_world
        payload["seeded_world_rules"] = (
            "The supplied world and app index are frozen. Never invent or alter starting records, "
            "policies or facts. initial_materials must enumerate ALL decisive existing records, "
            "one exact app_id.collection#id reference per string, with no additional prose. "
            "Each feature_cell collection must have at least one such reference. "
            "References name existing inputs, not future deliverables. Do not request reseeding. "
            # validate_feature_cells accepts a deviating cell and only reports it, but nothing said
            # so, and "use every cell exactly once" plus "never invent records" left refusal as the
            # designer's only legal move. Fourteen of fifteen mining attempts returned no workflows
            # with a selection_reason naming the collection it could not ground.
            "If an assigned feature_cell cannot be grounded in this frozen world, do not return an "
            "empty batch: keep the assigned outline and choose the nearest cell that IS grounded "
            "here, from collections that hold enough real records to carry the decision, distinct "
            "from the cells the company's existing tasks use. Name the substitution and the reason "
            "in selection_reason. A grounded task on a different cell is the wanted outcome; no "
            "task at all is the worst one."
        )
        payload["public_brief_rules"] = (
            "Give the boss a professional goal, scope, dates and completion expectations in 3–8 "
            "plain-English sentences. Leave analysis, answer values, grading internals and delegation "
            "plans private. All decisive constraints must be discoverable in the frozen workspace."
        )
        payload["standard_bundle_rule"] = (
            "Use the seeded standard workplace apps for chat/email delegation and reporting. "
            "They need not appear in software_requirement_ids; list required domain software."
        )
        payload["allow_amendment"] = allow_amendment = False
    matrix = None
    if seeded_world is not None or config.get("design", {}).get("feature_matrix", False):
        matrix = build_feature_matrix(
            company,
            catalogs,
            count,
            config.get("generation", {}).get("seed", 0),
            coverage,
            selected=selected,
            reserved=reserved_cells,
            available_collections=allowed_collections,
            difficulty_mix=config.get("generation", {}).get("difficulty_mix"),
        )
        payload["feature_matrix"] = matrix
        payload["feature_matrix_instruction"] = (
            "Design one task for each required feature_cell before inventing its story. "
            "Use every cell exactly once and preserve any assigned outline_id. Collections are app_id.key. "
            "These are decisive record collections, not an exhaustive list of all supporting app use. "
            "Difficulty targets steer the batch; choose an honest label if the requested level does not fit."
        )
        payload["difficulty_mix"] = difficulty_shares(config.get("generation", {}).get("difficulty_mix"))
    worker_floor = minimum_workers(getattr(models, "config", {}))
    execution_mode = getattr(models, "config", {}).get("design", {}).get("execution_mode", "scheduled")
    if seeded_world is not None:
        execution_mode = "digital"
        worker_floor = max(3, worker_floor)
    payload["execution_mode"] = execution_mode
    payload["available_runtime_apps"] = (
        getattr(models, "config", {}).get("design", {}).get("available_runtime_apps")
    )
    if mounting := runtime_mounting(getattr(models, "config", {})):
        payload["runtime_mounting"] = mounting
    payload["minimum_workers_per_workflow"] = worker_floor
    # Generated from the gate's own constants: the skill used to hand-copy the term list.
    payload["brief_gate"] = brief_gate()
    call_options = {}
    if read_only_tools:
        context = tool_context(catalogs, excerpts)
        if seeded_world is not None and seed_states:
            # read_seed is the designer's only way to read a record's body: the app index carries
            # ids, titles and dates, never contents. Without this key stage_tools.definitions()
            # does not register the tool, and 55 of 64 rejected drafts were empty batches whose
            # selection_reason said the index "omits their contents" or that no read_seed was
            # enabled. The states go in the tool context, which is a separate file the server
            # reads, so they cost nothing against the prompt ceiling.
            context["seed"] = seed_states
        call_options["context"] = context
        payload["available_tools"] = [tool["name"] for tool in definitions(context)]
        payload["source_previews"] = [
            {
                **s,
                "excerpt": s["excerpt"][:4000],
                "scope": "preview_use_read_source_for_full_text",
                "total_chars": len(s["excerpt"]),
            }
            for s in excerpts
        ]
    else:
        # Never offer truncated previews whose continuation tool is disabled.
        payload["source_excerpts"] = excerpts
    batch_type = FeatureDesignBatch if matrix is not None else DesignBatch
    schema = generation_schema(
        batch_type, workflow_version, worker_floor, execution_mode, matrix is not None, difficulty_required
    )
    fields = schema["$defs"]["Workflow"]["properties"]
    if seeded_world and seeded_world.get("accepted_world"):
        fields["assessment"] = {"$ref": "#/$defs/TaskAssessment"}
        # Co-design reads native records and writes the full task plus assessment.
        # Give it the configured authoring budget, not a short completion budget.
        if authoring_timeout := config.get("generation", {}).get("authoring_timeout_seconds"):
            call_options["timeout_seconds"] = authoring_timeout
    if matrix is not None:
        cell_fields = schema["$defs"]["FeatureCell"]["properties"]
        cell_fields["collections"]["items"]["enum"] = company_collections(company, catalogs)
        cell_fields["decision_type"]["enum"] = list(DECISION_TYPES)
    fields["company_id"]["enum"] = [company.id]
    fields["outline_id"]["enum"] = eligible
    fields["id"]["enum"] = [f"{company.id}_{oid}" for oid in eligible]
    # Constrain the author to the run's declared runtime surface when there is one, so no
    # research or design call is spent on apps the runtime cannot host.
    surface = getattr(models, "config", {}).get("design", {}).get("available_runtime_apps")
    schema["$defs"]["Software"]["properties"]["catalog_app_ids"]["items"]["enum"] = sorted(
        set(surface) & set(catalogs.apps) if surface else catalogs.apps
    )
    if not allow_amendment:
        schema["properties"]["amendment"] = {"type": "null"}
    batch, receipt = models.call(
        "expand",
        prompt + "\n" + json.dumps(payload, ensure_ascii=False),
        batch_type,
        schema=schema,
        **call_options,
    )
    cell_validation = None
    try:
        if matrix is not None:
            cell_validation = validate_feature_cells(
                batch.workflows, matrix, company_collections(company, catalogs)
            )
        amended = company
        if batch.amendment is not None:
            if not allow_amendment:
                raise ValueError("published dossiers cannot be amended")
            if not batch.amendment.reason.strip():
                raise ValueError("an amendment needs a substantive reason")
            replacement = batch.amendment.model_dump(exclude={"reason"})
            for field in replacement:
                if not {x.id for x in getattr(company, field)} <= {x["id"] for x in replacement[field]}:
                    raise ValueError(f"amendment cannot remove existing {field} IDs")
            amended = Company.model_validate({**company.model_dump(), **replacement})
            catalogs.validate(amended)
        unique(batch.workflows, "workflow")
        outline_ids = [w.outline_id for w in batch.workflows]
        if (
            len(outline_ids) != count
            or len(set(outline_ids)) != count
            or not set(outline_ids) <= set(eligible)
        ):
            # An empty batch is how the designer declines, and selection_reason is where it says
            # which record it could not find. Carry that into the error: the mining driver writes a
            # permanent "declined the spare outlines" marker off this message, and without the
            # reason a world is retired on a guess. Keep the leading phrase; the driver greps it.
            raise ValueError(
                "return the required number of distinct eligible outlines"
                + (
                    f" -- the designer returned {len(outline_ids)} and gave this reason: "
                    + " ".join(batch.selection_reason.split())
                    if batch.selection_reason.strip()
                    else ""
                )
            )
        if selected is not None and set(outline_ids) != set(selected):
            raise ValueError("fixed outline selection must be preserved")
        for workflow in batch.workflows:
            if difficulty_required and workflow.difficulty is None:
                raise ValueError("The authoring skill requires difficulty on every workflow")
            if workflow.execution_mode != execution_mode:
                raise ValueError("Workflow execution mode differs from the frozen run")
            validate_execution_support(amended, workflow, getattr(models, "config", {}))
            validate_workflow(amended, workflow, version=workflow_version, minimum_workers=worker_floor)
            check_plain_english(workflow)
            check_contribution_apps(
                amended,
                workflow,
                worker_apps=seeded_world["worker_apps"]
                if seeded_world and seeded_world.get("accepted_world")
                else None,
            )
            check_criteria_are_about_records(workflow)
            if workflow.id != f"{company.id}_{workflow.outline_id}":
                raise ValueError("workflow id must be company_id + '_' + outline_id")
    except ValueError as exc:
        raise ModelOutputInvalid(str(exc), batch.model_dump(), receipt) from exc
    return (
        amended,
        batch.workflows,
        receipt,
        {
            **(
                {"feature_matrix": matrix, "feature_validation": cell_validation}
                if matrix is not None
                else {}
            ),
            "selection_reason": batch.selection_reason,
            "amendment": batch.amendment.model_dump() if batch.amendment else None,
        },
    )


# Words that mark a brief written for a grader rather than said by a manager. A few are ordinary
# in some sectors; the gate fires on their accumulation, not on one.
REGISTER_TERMS = (
    "reconcil",
    "evidence",
    "evidentiary",
    "handoff",
    "retain",
    "retention",
    "predecessor",
    "consequential",
    "governing",
    "population",
    "disposition",
    "supersede",
    "internally approved",
    "specialist work",
    "integrate",
    "artifact",
    "mandate",
    "authority limit",
    "source version",
    "reference date",
    "using records available",
    "within the approved",
    "as applicable",
    "operationaliz",
    "substantive",
    "proportionate",
    "downstream",
    "leverag",
    "stakeholder",
)


# "Today", "this Friday", "by Monday": a task must read the same whichever real day it runs on.
RELATIVE_TIME = re.compile(
    r"\b(?:today|tomorrow|yesterday|tonight|this (?:week|month|morning|afternoon|monday|tuesday|wednesday|thursday|friday)"
    r"|next (?:week|month|monday|tuesday|wednesday|thursday|friday)|end of (?:the )?(?:day|week)"
    r"|by (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?!,? (?:january|february|march|april|may|june|july|august|september|october|november|december)))\b",
    re.IGNORECASE,
)
# Forms the regex above rejects, named for the author. A test keeps the two in step.
RELATIVE_TIME_EXAMPLES = (
    "today",
    "tonight",
    "tomorrow",
    "yesterday",
    "this week",
    "this Friday",
    "next month",
    "next Monday",
    "end of the week",
    "by Friday",
)
REGISTER_LIMIT = 3  # register terms that sink a brief; a title is sunk by one
SENTENCE_WORD_LIMIT = 28  # mean words per sentence
LONG_WORD_SHARE_LIMIT = 0.12  # share of words of twelve letters or more


def brief_gate():
    """The rule check_plain_english applies, stated for the author in the payload.

    The skill hand-copied the term list and carried 23 of the 29, so a brief rejected for
    "downstream", "leverage", "operationalize", "evidentiary", "retention" or "within the
    approved" was rejected on a word its instructions never named. Generated, it cannot drift.
    """
    return {
        "register_terms": sorted(REGISTER_TERMS),
        "register_terms_that_reject_a_brief": REGISTER_LIMIT,
        "register_terms_that_reject_a_title": 1,
        "max_mean_words_per_sentence": SENTENCE_WORD_LIMIT,
        "max_share_of_words_over_eleven_letters": LONG_WORD_SHARE_LIMIT,
        "relative_time_rejected_in": ["brief", "deliverables"],
        "relative_time_examples": list(RELATIVE_TIME_EXAMPLES),
    }


def brief_register(text):
    """Measure how a brief reads: sentence length, long words and grader-register terms."""
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", text)
    lowered = text.lower()
    return {
        "sentences": len(sentences),
        "mean_words": round(len(words) / max(1, len(sentences)), 1),
        "long_word_share": round(sum(len(w) >= 12 for w in words) / max(1, len(words)), 3),
        "register_terms": sorted(t for t in REGISTER_TERMS if t in lowered),
    }


def check_criteria_are_about_records(workflow):
    """A success criterion describes records the work changes, never how a screen is arranged.

    A criterion whose observable is a sort order or an open view ("currentSortDirection='asc'")
    can only be graded on interface state, which every worker can set without doing the work.
    """
    for index, criterion in enumerate(workflow.success_criteria):
        hits = interface_terms(f"{criterion.requirement} {criterion.observable}")
        if hits:
            raise ValueError(
                f"{workflow.id}: success criterion {index} is about interface state {hits}; "
                "describe the records the work changes instead"
            )


# Only technical key names count ("currentSortDirection", "viewMode"); plain words that also name
# interface state ("selected", "view", "title") are ordinary English.
INTERFACE_TERMS = frozenset(k for k in UI_STATE_KEYS if re.search(r"[A-Z_]", k))
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")


def interface_terms(text):
    return sorted(k for k in INTERFACE_TERMS if re.search(rf"\b{re.escape(k)}\b", text))


def strip_interface_state(observable):
    """Drop the sentences of an observable that describe interface state; keep the rest.

    Returns (text, dropped sentences). An observable that was only interface state comes back
    empty, and the caller falls back to the requirement.
    """
    kept, dropped = [], []
    for sentence in _SENTENCE.split(observable.strip()):
        (dropped if interface_terms(sentence) else kept).append(sentence)
    return " ".join(kept).strip(), dropped


def repair_criteria(folder):
    """Mechanically repair exported tasks whose criteria describe interface state.

    Records what changed under workflow.criteria_repair so the grader author and the reviewer
    see the criteria as they now stand. Returns {task_id: [criterion indexes repaired]}.
    """
    from .storage import read, write

    folder = Path(folder)
    repaired = {}
    for path in sorted(folder.glob("tasks/*/workflow.json")):
        if path.parent.name.startswith("_"):
            continue
        workflow = read(path)
        changes = []
        for index, criterion in enumerate(workflow.get("success_criteria", [])):
            text, dropped = strip_interface_state(criterion.get("observable", ""))
            if not dropped and not interface_terms(criterion.get("requirement", "")):
                continue
            requirement, req_dropped = strip_interface_state(criterion.get("requirement", ""))
            before = dict(criterion)
            criterion["observable"] = text or requirement or criterion["requirement"]
            criterion["requirement"] = requirement or criterion["requirement"]
            changes.append({"index": index, "before": before, "dropped": dropped + req_dropped})
        if changes:
            workflow["criteria_repair"] = {"at": now_iso(), "changes": changes}
            write(path, workflow)
            repaired[workflow.get("id", path.parent.name)] = [c["index"] for c in changes]
    return repaired


def now_iso():
    from .storage import now

    return now()


def check_contribution_apps(company, workflow, *, worker_apps=None):
    """Check app coverage and access, using frozen grants when the world already exists."""
    known = {a for software in company.software for a in software.catalog_app_ids} | STANDARD_APPS
    named = [c for c in workflow.contributions if c.apps]
    if not named and worker_apps is None:
        return
    if len(named) != len(workflow.contributions):
        raise ValueError(f"{workflow.id}: every contribution names apps, or none does")
    for c in named:
        unknown = set(c.apps) - known
        if unknown:
            raise ValueError(
                f"{workflow.id}: {c.worker_id} holds apps the company does not have: {sorted(unknown)}"
            )
        if worker_apps is not None:
            unavailable = set(c.apps) - set(worker_apps.get(c.worker_id, []))
            if unavailable:
                raise ValueError(
                    f"{workflow.id}: {c.worker_id} names apps outside frozen grants: {sorted(unavailable)}"
                )
    domain = known - STANDARD_APPS
    # Before seeding, app separation is a design constraint. Accepted worlds
    # already fix access; task assessment checks dependencies and input visibility.
    if worker_apps is None and domain and all(domain <= set(c.apps) for c in named):
        raise ValueError(f"{workflow.id}: every worker holds every domain app; at least one must lack one")
    manager = next((c for c in named if c.worker_id == workflow.manager_id), None)
    cell = getattr(workflow, "feature_cell", None)
    if cell is not None:
        # Legacy designs get standard apps implicitly. Frozen tasks must name
        # their granted apps, including the standard workplace tools.
        reachable = set().union(*(set(c.apps) for c in named))
        if worker_apps is None:
            reachable |= STANDARD_APPS
        unheld = sorted({c.split(".")[0] for c in cell.collections} - reachable)
        if unheld:
            raise ValueError(
                f"{workflow.id}: nobody on the team holds {unheld}, where the decisive collections "
                "live; give the app to the specialist whose contribution works in it"
            )
    if worker_apps is None and manager is not None and domain and cell is not None:
        reach = set(manager.apps) | STANDARD_APPS
        if all(c.split(".")[0] in reach for c in cell.collections):
            raise ValueError(
                f"{workflow.id}: the manager holds every app the decisive collections live in; "
                "a decisive collection must sit in a domain app only a specialist holds, so the task needs the team"
            )


def check_plain_english(workflow):
    """A brief a manager could have said aloud. Raises ValueError with the concrete fix."""
    gauge = brief_register(workflow.brief)
    problems = []
    if len(gauge["register_terms"]) >= REGISTER_LIMIT:
        problems.append(
            f"brief is written in the grader's register ({', '.join(gauge['register_terms'])}); "
            "say it the way the manager would say it to the team, in everyday words"
        )
    if gauge["mean_words"] > SENTENCE_WORD_LIMIT:
        problems.append(f"brief averages {gauge['mean_words']} words per sentence; use shorter sentences")
    if gauge["long_word_share"] > LONG_WORD_SHARE_LIMIT:
        problems.append(
            "brief leans on long abstract words; name the customer, the thing and the date instead"
        )
    relative = RELATIVE_TIME.findall(
        workflow.brief + " " + " ".join(getattr(workflow, "deliverables", None) or [])
    )
    if relative:
        problems.append(
            f"brief uses relative time ({', '.join(sorted({r.lower() for r in relative})[:4])}); the world is a snapshot at "
            "its reference date and the task must read the same on any day it is run, so name the date"
        )
    title_terms = [t for t in REGISTER_TERMS if t in workflow.title.lower()]
    if title_terms:
        problems.append(f"title uses {title_terms}; titles say what the team is doing for whom")
    if problems:
        raise ValueError(f"{workflow.id}: " + "; ".join(problems))
    return gauge


def expansion_schema(
    company,
    selected,
    workflow_version="1",
    minimum_workers=2,
    execution_mode="scheduled",
    difficulty_required=False,
):
    """Expose known registries as enums instead of asking the author to guess IDs."""
    schema = generation_schema(
        WorkflowBatch,
        workflow_version,
        minimum_workers,
        execution_mode,
        difficulty_required=difficulty_required,
    )
    defs = schema["$defs"]
    workflow = defs["Workflow"]["properties"]
    workflow["company_id"]["enum"] = [company.id]
    workflow["outline_id"]["enum"] = selected
    workflow["id"]["enum"] = [f"{company.id}_{oid}" for oid in selected]
    for field, ids in (
        ("worker_ids", [w.id for w in company.workers]),
        ("team_ids", [t.id for t in company.teams]),
        ("software_requirement_ids", [s.id for s in company.software]),
        ("evidence_ids", [e.id for e in company.evidence]),
    ):
        workflow[field]["items"]["enum"] = ids
    workers = [w.id for w in company.workers]
    if execution_mode == "digital":
        workflow["manager_id"]["enum"] = workers
    defs["Phase"]["properties"]["worker_ids"]["items"]["enum"] = workers
    defs["Contribution"]["properties"]["worker_id"]["enum"] = workers
    defs["Contribution"]["properties"]["shares_with"]["items"]["enum"] = workers
    defs["Event"]["properties"]["recipients"]["items"]["enum"] = workers
    return schema


def expand(
    models,
    prompt,
    company,
    evidence,
    selected,
    feedback="",
    *,
    workflow_version="1",
    seeded_world=None,
    catalogs=None,
):
    if seeded_world is not None:
        if catalogs is None:
            raise ValueError("seeded expansion requires catalogs for the feature matrix")
        _, tasks, receipt, _ = design(
            models,
            prompt,
            company,
            evidence,
            catalogs,
            [],
            len(selected),
            selected=selected,
            feedback=feedback,
            allow_amendment=False,
            read_only_tools=False,
            workflow_version="2",
            seeded_world=seeded_world,
        )
        return tasks, receipt
    worker_floor = minimum_workers(getattr(models, "config", {}))
    execution_mode = getattr(models, "config", {}).get("design", {}).get("execution_mode", "scheduled")
    payload = {
        "company": company.model_dump(),
        "evidence_status": evidence,
        "selected_outline_ids": selected,
        "eligible_outline_ids": selected,
        "required_count": len(selected),
        "fixed_selection": True,
        "allow_amendment": False,
        "available_tools": [],
        "revision_feedback": feedback,
        "minimum_workers_per_workflow": worker_floor,
        "execution_mode": execution_mode,
        "available_runtime_apps": getattr(models, "config", {})
        .get("design", {})
        .get("available_runtime_apps"),
    }
    if mounting := runtime_mounting(getattr(models, "config", {})):
        payload["runtime_mounting"] = mounting
    difficulty_required = bool(
        getattr(models, "config", {}).get("generation", {}).get("difficulty_required", False)
    )
    batch, receipt = models.call(
        "expand",
        prompt + "\n" + json.dumps(payload, ensure_ascii=False),
        WorkflowBatch,
        schema=expansion_schema(
            company, selected, workflow_version, worker_floor, execution_mode, difficulty_required
        ),
    )
    try:
        unique(batch.workflows, "workflow")
        if {w.outline_id for w in batch.workflows} != set(selected) or len(batch.workflows) != len(selected):
            raise ValueError("expansion must return one workflow per requested outline")
        for workflow in batch.workflows:
            if difficulty_required and workflow.difficulty is None:
                raise ValueError("The authoring skill requires difficulty on every workflow")
            if workflow.execution_mode != execution_mode:
                raise ValueError("Workflow execution mode differs from the frozen run")
            validate_execution_support(company, workflow, getattr(models, "config", {}))
            validate_workflow(company, workflow, version=workflow_version, minimum_workers=worker_floor)
            if workflow.id != f"{company.id}_{workflow.outline_id}":
                raise ValueError("workflow id must be company_id + '_' + outline_id")
    except ValueError as exc:
        raise ModelOutputInvalid(str(exc), batch.model_dump(), receipt) from exc
    return batch.workflows, receipt
