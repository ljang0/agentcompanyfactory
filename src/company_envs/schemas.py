"""The Stage 1 / world-builder boundary. These describe designs, not executed worlds."""

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The workplace bundle every company and every worker gets: mail, chat, calendar, drive, docs.
STANDARD_APPS = frozenset(
    {"gmail_mock", "slack_mock", "google_calendar_mock", "google_drive_mock", "google_docs_mock"}
)

Id = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]
WorkflowId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,160}$")]
Text = Annotated[str, Field(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(Record):
    id: Id
    real_firm: Text
    sector: Text
    website: Text
    reason: Text


class Discovery(Record):
    candidates: list[Candidate]


class Claim(Record):
    id: Id
    kind: Literal["sourced", "inferred", "synthetic"]
    text: Text
    source_url: str
    quote: str
    basis: str


class Worker(Record):
    id: Id
    title: Text
    soc: Annotated[str, Field(pattern=r"^\d{2}-\d{4}$")]
    responsibility: Text
    information_access: list[Text]
    authority: Text
    evidence_ids: list[Id]


class Team(Record):
    id: Id
    name: Text
    purpose: Text
    worker_ids: Annotated[list[Id], Field(min_length=1)]


class Software(Record):
    id: Id
    capability: Text
    reference_product: str
    catalog_app_ids: list[str]
    status: Literal["catalog_candidate", "substitution", "gap"]
    rationale: Text
    evidence_ids: list[Id]


class Outline(Record):
    id: Id
    title: Text
    objective: Text
    decision_problem: Text
    team_ids: list[Id] = Field(
        description="Contributing team IDs. Every worker_id must belong to at least one of these teams."
    )
    worker_ids: list[Id]
    evidence_ids: list[Id]


def unique(items, label):
    ids = [x.id for x in items]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate {label} ids")
    return set(ids)


def references(values, known, label):
    missing = set(values) - set(known)
    if missing:
        raise ValueError(f"{label} refers to unknown ids: {sorted(missing)}")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} repeats an id")


class Company(Record):
    schema_version: Literal["1"] = "1"
    id: Id
    name: Text
    real_firm: Text
    website: Text
    sector: Text
    naics: Annotated[str, Field(pattern=r"^\d{2,6}$")]
    location: Text
    operations: Text
    evidence: Annotated[list[Claim], Field(min_length=1)]
    workers: Annotated[list[Worker], Field(min_length=2)]
    teams: Annotated[list[Team], Field(min_length=1)]
    software: Annotated[list[Software], Field(min_length=1)]
    outlines: Annotated[list[Outline], Field(min_length=1)]
    assumptions: list[str]

    @model_validator(mode="after")
    def links(self):
        ev = unique(self.evidence, "evidence")
        ws = unique(self.workers, "worker")
        ts = unique(self.teams, "team")
        unique(self.software, "software")
        unique(self.outlines, "outline")
        members = set()
        for t in self.teams:
            references(t.worker_ids, ws, f"team {t.id}")
            members.update(t.worker_ids)
        if ws != members:
            raise ValueError("every worker must belong to a team")
        for x in [*self.workers, *self.software, *self.outlines]:
            references(x.evidence_ids, ev, x.id)
        for o in self.outlines:
            references(o.team_ids, ts, o.id)
            references(o.worker_ids, ws, o.id)
            team_workers = {w for t in self.teams if t.id in o.team_ids for w in t.worker_ids}
            missing_members = set(o.worker_ids) - team_workers
            if missing_members:
                available = {
                    wid: [t.id for t in self.teams if wid in t.worker_ids] for wid in sorted(missing_members)
                }
                raise ValueError(
                    f"outline {o.id}: workers {sorted(missing_members)} are outside its team_ids {o.team_ids}. Include the relevant existing team IDs {available}, or remove workers not needed by this outline."
                )
        return self


class Phase(Record):
    id: Id
    name: Text
    worker_ids: Annotated[list[Id], Field(min_length=1)]
    depends_on: list[Id]
    inputs: Annotated[list[Text], Field(min_length=1)]
    outputs: Annotated[list[Text], Field(min_length=1)]
    decision: Text
    downstream_effect: Text
    when: str = Field(
        default="",
        exclude_if=lambda value: not value,
        description="Applicability condition; 'always' for unconditional work. V2 only.",
    )


class Contribution(Record):
    worker_id: Id
    inputs: list[Text]
    work: Text
    produces: list[Text]
    shares_with: list[Id]
    # Apps this worker's VM is logged into for the task (standard bundle implied). When every
    # contribution names them, seeding gives each worker only those apps, so work that lives in a
    # system a worker lacks has to be delegated to the worker who has it.
    apps: list[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$")]
    ] = []  # app ids keep their case (Zendesk_mock)


class Event(Record):
    id: Id
    after_phase: Id
    trigger: Text
    recipients: list[Id]
    effect: Text


class Criterion(Record):
    requirement: Text
    observable: Text
    method: Literal["state", "artifact", "judgment"]


class Effort(Record):
    minimum_hours: float = Field(gt=0)
    maximum_hours: float = Field(gt=0)
    basis: Text

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum_hours < self.minimum_hours:
            raise ValueError("effort maximum must be at least its minimum")
        return self


class Outcome(Record):
    id: Id
    when: Text
    required_phase_ids: list[Id]
    observable: Text


class WitnessStep(Record):
    phase_id: Id | None = Field(default=None, exclude_if=lambda value: value is None)
    action_and_result: Text


class NumericCheck(Record):
    constraint: Text
    left: Text
    relation: Literal["<=", ">=", "=="]
    right: Text
    unit: Text

    @model_validator(mode="after")
    def arithmetic(self):
        from .stage_tools import calculate

        try:
            left, right = calculate(self.left)["value"], calculate(self.right)["value"]
        except (ValueError, SyntaxError, ArithmeticError) as exc:
            raise ValueError(f"{self.constraint}: invalid bounded arithmetic: {exc}") from exc
        holds = {
            "<=": left <= right,
            ">=": left >= right,
            "==": math.isclose(left, right, rel_tol=0, abs_tol=1e-9),
        }[self.relation]
        if not holds:
            raise ValueError(f"{self.constraint}: {left} {self.relation} {right} {self.unit} is false")
        return self


class FeasiblePath(Record):
    """Builder/reviewer-only example, never a worker script or sole grading key."""

    outcome_id: Id
    steps: Annotated[list[WitnessStep], Field(min_length=1)]
    constraint_checks: Annotated[list[Text], Field(min_length=1)]
    numeric_checks: list[NumericCheck]


class CompletionContract(Record):
    outcomes: Annotated[list[Outcome], Field(min_length=1)]
    feasible_path: FeasiblePath


class AssessmentCriterion(Record):
    criterion: int = Field(ge=1, description="One-based success_criteria index")
    weight: float = Field(gt=0, allow_inf_nan=False)
    must_pass: bool
    evidence: list[Text] = Field(min_length=1, description="Exact source record references")
    rubric: Text
    failure_cases: list[Text] = Field(min_length=1)


class WorkerDependency(Record):
    worker_id: Id
    inputs: list[Text] = Field(min_length=1, description="Exact references visible to this worker")
    produces: Text
    consumed_by: list[Id] = Field(min_length=1)
    necessity: Text
    authority_sources: list[Text]


class TaskAssessment(Record):
    criteria: list[AssessmentCriterion] = Field(min_length=1)
    dependencies: list[WorkerDependency] = Field(min_length=3)
    alternative_successes: list[Text]
    prohibited_shortcuts: list[Text] = Field(min_length=1)


class Workflow(Record):
    schema_version: Literal["1", "2"] = "1"
    difficulty: Literal["easy", "medium", "hard"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Easy: one worker could finish in under an hour with the records in front of them. "
            "Medium: needs two workers and a judgement call. "
            "Hard: long-horizon work with three or more workers, cross-app state tracking, "
            "and a decision under conflicting inputs."
        ),
    )
    execution_mode: Literal["scheduled", "digital"] = Field(
        default="scheduled", exclude_if=lambda value: value == "scheduled"
    )
    manager_id: Id | None = Field(default=None, exclude_if=lambda value: value is None)
    feature_cell: dict[str, list[str] | str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    id: WorkflowId
    company_id: Id
    outline_id: Id
    title: Text
    brief: Text
    objective: Text
    decision_problem: Text
    canonical_description: Text
    team_ids: Annotated[list[Id], Field(min_length=1)]
    worker_ids: Annotated[
        list[Id],
        Field(
            min_length=2,
            description="All participating worker IDs, including every phase worker and every contributor; each must belong to a listed team.",
        ),
    ]
    phases: list[Phase]
    contributions: list[Contribution]
    events: list[Event]
    initial_materials: Annotated[list[Text], Field(min_length=1)]
    deliverables: Annotated[list[Text], Field(min_length=1)]
    success_criteria: Annotated[list[Criterion], Field(min_length=1)]
    software_requirement_ids: Annotated[list[Id], Field(min_length=1)]
    evidence_ids: list[Id]
    calendar_days: int | None = Field(default=None, gt=0)
    estimated_human_effort: Effort
    assumptions: list[str]
    completion: CompletionContract | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Builder/reviewer-only completion and feasibility evidence. Required in V2.",
    )
    assessment: TaskAssessment | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Private assessment co-authored with a task discovered in an accepted world",
    )

    @model_validator(mode="after")
    def graph(self):
        digital = self.execution_mode == "digital"
        if digital:
            if self.schema_version != "2" or len(self.worker_ids) < 3:
                raise ValueError("Digital tasks require V2 and at least three consequential workers")
            if self.manager_id not in self.worker_ids:
                raise ValueError("Digital tasks require a manager from the participating workers")
            if self.events or self.calendar_days is not None:
                raise ValueError("Digital tasks cannot schedule events or simulated calendar duration")
        elif self.manager_id is not None or self.calendar_days is None or len(self.phases) < 2:
            raise ValueError("Scheduled workflows require calendar duration and at least two phases")
        phase_ids = unique(self.phases, "phase")
        unique(self.events, "event")
        references(self.worker_ids, set(self.worker_ids), "workflow workers")
        seen = set()
        for p in self.phases:
            references(p.worker_ids, self.worker_ids, p.id)
            references(p.depends_on, phase_ids, p.id)
            if not set(p.depends_on) <= seen:
                raise ValueError("phases must be in dependency order, without cycles; unroll rework rounds")
            seen.add(p.id)
        if not digital and not any(p.depends_on for p in self.phases):
            raise ValueError("workflow needs a dependency between phases")
        covered = {w for p in self.phases for w in p.worker_ids}
        if not digital and covered != set(self.worker_ids):
            raise ValueError("every participating worker must carry a phase")
        if digital:
            covered = set(self.worker_ids)
        contributors = [c.worker_id for c in self.contributions]
        if len(contributors) != len(set(contributors)) or set(contributors) != covered:
            raise ValueError("one contribution per participating worker is required")
        for c in self.contributions:
            references(c.shares_with, covered, c.worker_id)
        for e in self.events:
            references([e.after_phase], phase_ids, e.id)
            references(e.recipients, covered, e.id)
        if self.schema_version == "1":
            if self.completion is not None or any(p.when for p in self.phases):
                raise ValueError("completion and phase conditions require workflow schema_version 2")
        else:
            if self.completion is None or any(not p.when.strip() for p in self.phases):
                raise ValueError(
                    "V2 requires completion and a nonempty applicability condition for every phase"
                )
            outcomes = unique(self.completion.outcomes, "outcome")
            for outcome in self.completion.outcomes:
                if not digital and not outcome.required_phase_ids:
                    raise ValueError("Scheduled outcomes require at least one phase")
                references(outcome.required_phase_ids, phase_ids, outcome.id)
            witness = self.completion.feasible_path
            references([witness.outcome_id], outcomes, "feasible outcome")
            if not digital and any(step.phase_id is None for step in witness.steps):
                raise ValueError("Scheduled feasibility steps require phase IDs")
            steps = [step.phase_id for step in witness.steps if step.phase_id is not None]
            references(steps, phase_ids, "feasible path")
            done = set()
            phases = {p.id: p for p in self.phases}
            for phase_id in steps:
                if not set(phases[phase_id].depends_on) <= done:
                    raise ValueError(f"feasible path skips or precedes a hard prerequisite for {phase_id}")
                done.add(phase_id)
            outcome = next(o for o in self.completion.outcomes if o.id == witness.outcome_id)
            if not set(outcome.required_phase_ids) <= done:
                raise ValueError("feasible path omits a required phase of its selected outcome")
        return self

    def assignment(self):
        """Allowlisted worker-facing fields. Prose leakage still needs semantic review."""
        return {
            "workflow_id": self.id,
            "company_id": self.company_id,
            "title": self.title,
            "brief": self.brief,
        }


class WorkflowBatch(Record):
    workflows: Annotated[list[Workflow], Field(min_length=1)]


class TaskVerdict(Record):
    workflow_id: WorkflowId
    verdict: Literal["accept", "modify_brief", "revise", "reject"]
    severity: Literal["P0", "P1", "P2", "P3"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="P0 rejects; P1 fixes the brief; P2 accepts or fixes; P3 accepts. Null is for old reviews.",
    )
    brief_fix: str = Field(
        default="",
        exclude_if=lambda value: value == "",
        description="The full replacement brief, only for modify_brief.",
    )
    quality: int = Field(ge=1, le=5)
    novelty: Literal["distinct", "variant"]
    duplicate_of: str
    reasons: list[Text]


class Review(Record):
    company_verdict: Literal["accept", "revise", "reject"]
    company_reasons: list[Text]
    tasks: Annotated[list[TaskVerdict], Field(min_length=1)]


def minimum_workers(config):
    """Run-scoped policy; historical configs retain the original two-worker floor."""
    value = config.get("design", {}).get("minimum_workers", 2)
    if type(value) is not int or value < 2:
        raise ValueError("minimum_workers must be an integer of at least 2")
    return value


def validate_workflow(company: Company, workflow: Workflow, version=None, minimum_workers=2):
    if len(workflow.worker_ids) < minimum_workers:
        raise ValueError(f"workflow requires at least {minimum_workers} participating workers")
    if version is not None and workflow.schema_version != version:
        raise ValueError(f"run requires workflow schema_version {version}, got {workflow.schema_version}")
    if workflow.company_id != company.id:
        raise ValueError("workflow belongs to another company")
    references([workflow.outline_id], {o.id for o in company.outlines}, "outline")
    references(workflow.team_ids, {t.id for t in company.teams}, "workflow teams")
    members = {w for t in company.teams if t.id in workflow.team_ids for w in t.worker_ids}
    references(workflow.worker_ids, members, "workflow workers")
    references(workflow.software_requirement_ids, {a.id for a in company.software}, "software")
    references(workflow.evidence_ids, {e.id for e in company.evidence}, "workflow evidence")
