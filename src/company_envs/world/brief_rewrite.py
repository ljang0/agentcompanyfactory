"""Reword a company's accepted task briefs in the manager's voice; facts and grading stay fixed.

Stage 1 briefs came out in the grader's register ("using records available September 8,
prepare an internally approved response..."). This pass asks the company-plain-brief skill
for the same tasks said plainly, checks that every number and date survived and that the
result passes the register gate, then rewrites workflow.json and assignment.json in the
company folder with the previous text kept beside them. Nothing about what a task asks for,
its feature cell, materials or completion path changes.
"""

import json
import re
from pathlib import Path

from pydantic import BaseModel

from company_envs.config import load_config
from company_envs.models import ModelOutputInvalid, Models
from company_envs.receipt import Receipt
from company_envs.storage import digest, now, read, write
from company_envs.workflows import REGISTER_TERMS, brief_gate, brief_register, check_plain_english

from .state_seed import load_skill

SKILL = "company-plain-brief"
HARD_FACT = re.compile(
    r"\$?\d[\d,]*(?:\.\d+)?%?"
    r"|\b(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}\b"
)


class TaskText(BaseModel):
    workflow_id: str
    title: str
    brief: str
    deliverables: list[str]
    requirements: list[str]


class RewrittenBriefs(BaseModel):
    tasks: list[TaskText]


def hard_facts(text):
    return {f.strip(",.") for f in HARD_FACT.findall(text)}


def publish_assignment(folder, workflow_id, company_id, title, brief):
    """Keep the public boss brief in step with the task text, creating it if it is missing."""
    path = Path(folder) / "tasks" / workflow_id / "assignment.json"
    assignment = read(path) if path.is_file() else {"workflow_id": workflow_id, "company_id": company_id}
    assignment.update(title=title, brief=brief)
    write(path, assignment)


def before_text(workflow):
    """The task's own words, in the shape the call receives and the restore path reads back."""
    return {
        "title": workflow["title"],
        "brief": workflow["brief"],
        "deliverables": workflow.get("deliverables", []),
        "requirements": [c["requirement"] for c in workflow.get("success_criteria", [])],
    }


def register_problems(texts):
    """Deliverables and requirements still carrying the grader's vocabulary."""
    problems = []
    for text in texts:
        hits = [t for t in REGISTER_TERMS if t in text.lower()]
        if len(hits) >= 2:
            problems.append(
                f"deliverable or requirement still in the grader's register ({', '.join(hits)}): {text[:60]!r}"
            )
    return problems


def rewrite_needed(workflow):
    """Why this task needs rewording, or nothing when its own text already passes the gate.

    52 of the 99 rewritten companies had no failing brief, title, deliverable or
    requirement: the call reworded accepted text, and every reword is another chance
    to drop a number. The same gate decides whether to ask and whether to accept.
    """

    class View:
        id = workflow["id"]
        title = workflow["title"]
        brief = workflow["brief"]

    problems = []
    try:
        check_plain_english(View())
    except ValueError as exc:
        problems.append(str(exc))
    return problems + register_problems(
        [
            *workflow.get("deliverables", []),
            *(c["requirement"] for c in workflow.get("success_criteria", [])),
        ]
    )


def check_rewrite(old, new):
    """Same task, plainer words: counts preserved, numbers and dates preserved, gate passed."""
    problems = []
    if len(new.deliverables) != len(old.get("deliverables", [])):
        problems.append("keep the same number of deliverables, in order")
    if len(new.requirements) != len(old.get("success_criteria", [])):
        problems.append("keep the same number of acceptance requirements, in order")
    new_text = " ".join([new.brief, *new.deliverables, *new.requirements])
    lost = hard_facts(old["brief"]) - hard_facts(new_text)
    if lost:
        problems.append(f"numbers or dates dropped from the brief: {sorted(lost)[:6]}")
    # Each requirement is later read by the grader author on its own, so its facts must survive
    # in that requirement, not merely somewhere in the brief.
    for index, (criterion, requirement) in enumerate(
        zip(old.get("success_criteria", []), new.requirements, strict=False)
    ):
        missing = hard_facts(criterion.get("requirement", "")) - hard_facts(requirement)
        if missing:
            problems.append(f"requirement {index + 1} lost its numbers or dates: {sorted(missing)[:4]}")

    class View:
        id = old["id"]
        title = new.title
        brief = new.brief

    try:
        check_plain_english(View())
    except ValueError as exc:
        problems.append(str(exc))
    return problems + register_problems([*new.deliverables, *new.requirements])


def restore_briefs(folder):
    """Put the pre-rewrite text back so a rewrite can start again from the original."""
    restored = 0
    for path in sorted(Path(folder).glob("tasks/*/workflow.json")):
        w = read(path)
        before = w.pop("plain_brief", {}).get("before")
        if not before:
            continue
        w["title"], w["brief"], w["deliverables"] = before["title"], before["brief"], before["deliverables"]
        for criterion, requirement in zip(
            w.get("success_criteria", []), before["requirements"], strict=False
        ):
            criterion["requirement"] = requirement
        write(path, w)
        assignment_path = path.parent / "assignment.json"
        if assignment_path.is_file():
            assignment = read(assignment_path)
            assignment.update(title=before["title"], brief=before["brief"])
            write(assignment_path, assignment)
        restored += 1
    marker = Path(folder) / "tasks" / "_plain_brief" / "REWRITE.json"
    if marker.is_file():
        marker.unlink()
    return restored


def rewrite_briefs(root, folder, models=None, again=False, only=None):
    """Reword every task of the company, or, with ONLY, the named ids alone.

    ONLY is the mined-task path: tasks authored after the company-wide pass carry no plain brief,
    and the pass itself will not run again because REWRITE.json is there. Naming ids rewrites just
    those (skipping any that already have one) and leaves REWRITE.json untouched -- it is an input
    of the assign-apps stage, whose marker is in turn an input of the seed stage, so re-dating it
    would re-seed an accepted world.
    """
    root, folder = Path(root), Path(folder)
    marker = folder / "tasks" / "_plain_brief" / "REWRITE.json"
    if only is None:
        if again:
            restore_briefs(folder)
        if marker.is_file():
            # A pass: the rewrite this marker names is on disk. Said in the shared word rather than
            # as ``tasks: 0`` with a bare ``skipped``, which is the shape a run that rewrote nothing
            # would also have had.
            return {**Receipt.passed(tasks=0).body, "skipped": "already rewritten"}
    config = load_config(root)
    models = models if models is not None else Models(config, folder / "tasks" / "_plain_brief")
    instructions, skill = load_skill(root, SKILL)
    company = read(folder / "company.json")
    workflows = {
        p.parent.name: read(p)
        for p in sorted((folder / "tasks").glob("*/workflow.json"))
        if not p.parent.name.startswith("_")
    }
    if only is not None:
        unknown = sorted(set(only) - set(workflows))
        if unknown:
            raise ValueError(f"{folder} has no such tasks: {unknown}")
        workflows = {w: t for w, t in workflows.items() if w in set(only) and "plain_brief" not in t}
        if not workflows:
            return {**Receipt.passed(tasks=0).body, "skipped": "already rewritten"}
    if not workflows:
        raise ValueError(f"{folder} has no tasks to rewrite")
    # A task whose own words already pass the gate is recorded as kept, not sent to the
    # model: the driver reads the plain_brief key to decide a task is prepared, so a
    # silent skip would ask for this company again on every pass.
    kept = [wid for wid, w in workflows.items() if not rewrite_needed(w)]
    for wid in kept:
        workflow = workflows.pop(wid)
        workflow["plain_brief"] = {
            "rewritten_at": now(),
            "skill": skill,
            "kept": "the task already reads as the manager would say it",
            "before": before_text(workflow),
        }
        write(folder / "tasks" / wid / "workflow.json", workflow)
        # The public brief still has to exist and agree: every later stage reads it, and
        # hub_vm refuses a task whose assignment.json is missing or blank.
        publish_assignment(folder, wid, company["id"], workflow["title"], workflow["brief"])
    attempt, receipt, by_id = 0, None, {}
    if workflows:
        payload = {
            "call": "plain_brief",
            "company": {k: company.get(k) for k in ("name", "sector", "operations", "location")},
            "workers": [
                {k: w.get(k) for k in ("id", "name", "title", "team_id")} for w in company.get("workers", [])
            ],
            "tasks": [{"workflow_id": wid, **before_text(w)} for wid, w in workflows.items()],
            # check_rewrite runs the same gate on the result, and the skill names only the words.
            # A manager's voice reaches for "by Friday", which the relative-time rule rejects.
            "brief_gate": brief_gate(),
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
            result, receipt = models.call("expand", prompt, RewrittenBriefs)
            by_id = {t.workflow_id: t for t in result.tasks}
            if set(by_id) != set(workflows) or len(result.tasks) != len(workflows):
                problems = ["return every task exactly once with its workflow_id unchanged"]
            else:
                problems = [
                    f"{wid}: {p}" for wid, w in workflows.items() for p in check_rewrite(w, by_id[wid])
                ]
            if not problems:
                break
            feedback = problems
        else:
            raise ModelOutputInvalid("; ".join(problems[:8]), result.model_dump(), receipt)

    for wid, w in workflows.items():
        new = by_id[wid]
        w["plain_brief"] = {"rewritten_at": now(), "skill": skill, "before": before_text(w)}
        w["title"], w["brief"], w["deliverables"] = new.title, new.brief, new.deliverables
        for criterion, requirement in zip(w.get("success_criteria", []), new.requirements, strict=True):
            criterion["requirement"] = requirement
        write(folder / "tasks" / wid / "workflow.json", w)
        publish_assignment(folder, wid, company["id"], new.title, new.brief)
    if only is None:
        # ``passed``, and like ASSIGN.json this marker turns out to have no other reachable outcome:
        # a company with no tasks raises above, and ``attempts: 0`` is reached only when every brief
        # already read plainly, in which case ``kept`` names all of them. So ``attempts: 0`` with an
        # empty ``kept`` -- the one shape that would be an empty run wearing a pass -- cannot be
        # written. Measured 2026-09-10: 0 of the 99 REWRITE.json on disk hold ``attempts: 0``, so
        # even the legitimate version has never occurred. The word and the test beside it make the
        # guarantee checkable instead of leaving it implied by a raise and a list comprehension.
        write(
            marker,
            {
                "rewritten_at": now(),
                **Receipt.passed().body,
                "skill": skill,
                "receipt": receipt,
                "attempts": attempt + 1 if workflows else 0,
                "kept": kept,
                "register": {wid: brief_register(by_id[wid].brief) for wid in workflows},
            },
        )
    manifest = read(folder / "MANIFEST.json")
    manifest["hashes"] = {
        str(p.relative_to(folder)): digest(p.read_bytes()) for p in sorted(folder.rglob("*")) if p.is_file()
    }
    write(folder / "MANIFEST.json", manifest)
    return {
        **Receipt.passed().body,
        "tasks": len(workflows),
        "kept": len(kept),
        "attempts": attempt + 1 if workflows else 0,
    }
