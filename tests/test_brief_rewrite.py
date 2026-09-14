import json

import pytest

from company_envs.models import ModelOutputInvalid
from company_envs.world.brief_rewrite import RewrittenBriefs, TaskText, check_rewrite, rewrite_briefs

OLD = {
    "id": "acme_o1",
    "title": "Reconcile the governing lease evidence",
    "deliverables": ["A reply", "A forecast"],
    "success_criteria": [
        {"requirement": "Reconcile all governing evidence", "observable": "x", "method": "y"}
    ],
    "brief": (
        "Using records available September 8, prepare an internally approved response to Birch Instruments, "
        "reconcile the governing lease against $4,200 of 2025 charges, and integrate consequential specialist "
        "work within the approved authority limits by September 15."
    ),
}
PLAIN = TaskText(
    workflow_id="acme_o1",
    title="Answer Birch Instruments' expense dispute",
    brief=(
        "Birch Instruments is disputing $4,200 of their 2025 expense charges. Work out which charges they can "
        "actually challenge and what their forecast should say, as of September 8. Get them a written answer by September 15."
    ),
    deliverables=["A written reply to Birch Instruments", "An updated forecast"],
    requirements=["The reply tells Birch which charges they can challenge and why"],
)


def test_check_rewrite_accepts_plain_faithful_text_and_rejects_lost_facts():
    assert check_rewrite(OLD, PLAIN) == []
    lossy = PLAIN.model_copy(update={"brief": "Birch Instruments is disputing charges. Answer them soon."})
    problems = check_rewrite(OLD, lossy)
    assert any("dropped" in p for p in problems)
    still_grader = PLAIN.model_copy(update={"brief": OLD["brief"]})
    assert any("register" in p for p in check_rewrite(OLD, still_grader))


class Models:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def call(self, job, prompt, response_type):
        self.prompts.append(prompt)
        return self.answers.pop(0), {"model": "fake"}


def make_company(tmp_path):
    root = tmp_path / "root"
    (root / ".agents" / "skills" / "company-plain-brief").mkdir(parents=True)
    (root / ".agents" / "skills" / "company-plain-brief" / "SKILL.md").write_text(
        "---\nname: company-plain-brief\n---\nSay it plainly.\n"
    )
    (root / "config.toml").write_text("[models]\n")
    folder = root / "companies" / "acme"
    (folder / "tasks" / "acme_o1").mkdir(parents=True)
    (folder / "company.json").write_text(
        json.dumps(
            {
                "id": "acme",
                "name": "Acme",
                "sector": "Real Estate",
                "workers": [{"id": "w1", "name": "Jo", "title": "Manager", "team_id": "t1"}],
            }
        )
    )
    (folder / "MANIFEST.json").write_text(json.dumps({"company_id": "acme", "hashes": {}}))
    (folder / "tasks" / "acme_o1" / "workflow.json").write_text(json.dumps(OLD))
    (folder / "tasks" / "acme_o1" / "assignment.json").write_text(
        json.dumps({"workflow_id": "acme_o1", "title": OLD["title"], "brief": OLD["brief"]})
    )
    return root, folder


def test_rewrite_briefs_rewrites_workflow_and_assignment_keeping_the_old_text(tmp_path):
    root, folder = make_company(tmp_path)
    models = Models([RewrittenBriefs(tasks=[PLAIN])])
    result = rewrite_briefs(root, folder, models=models)
    assert result["tasks"] == 1
    workflow = json.loads((folder / "tasks" / "acme_o1" / "workflow.json").read_text())
    assert workflow["brief"] == PLAIN.brief and workflow["plain_brief"]["before"]["brief"] == OLD["brief"]
    assert workflow["success_criteria"][0]["requirement"] == PLAIN.requirements[0]
    assert workflow["success_criteria"][0]["observable"] == "x"
    assert json.loads((folder / "tasks" / "acme_o1" / "assignment.json").read_text())["brief"] == PLAIN.brief
    assert (folder / "tasks" / "_plain_brief" / "REWRITE.json").is_file()
    assert rewrite_briefs(root, folder, models=models)["skipped"]


def test_only_rewrites_the_named_tasks_and_leaves_the_company_marker_alone(tmp_path):
    """The mined-task path: REWRITE.json is the apps stage's input, and re-dating it re-seeds."""
    root, folder = make_company(tmp_path)
    rewrite_briefs(root, folder, models=Models([RewrittenBriefs(tasks=[PLAIN])]))
    marker = folder / "tasks" / "_plain_brief" / "REWRITE.json"
    stamped = marker.stat().st_mtime_ns
    mined = folder / "tasks" / "acme_o2"
    mined.mkdir()
    (mined / "workflow.json").write_text(json.dumps({**OLD, "id": "acme_o2"}))
    models = Models([RewrittenBriefs(tasks=[PLAIN.model_copy(update={"workflow_id": "acme_o2"})])])
    assert rewrite_briefs(root, folder, models=models, only=["acme_o2"])["tasks"] == 1
    assert json.loads((mined / "workflow.json").read_text())["brief"] == PLAIN.brief
    assert json.loads((mined / "assignment.json").read_text())["brief"] == PLAIN.brief
    assert "acme_o1" not in models.prompts[0]  # the tasks that already read plainly are not re-read
    assert marker.stat().st_mtime_ns == stamped
    # A task that already has its plain brief is not rewritten twice, and no model is called.
    assert rewrite_briefs(root, folder, models=Models([]), only=["acme_o2"])["skipped"]
    with pytest.raises(ValueError, match="no such tasks"):
        rewrite_briefs(root, folder, models=Models([]), only=["acme_o9"])


def test_a_task_that_already_passes_the_gate_is_kept_instead_of_reworded(tmp_path):
    """52 of the 99 rewritten companies had nothing failing; their call reworded accepted text."""
    root, folder = make_company(tmp_path)
    clean = {
        **OLD,
        "id": "acme_o2",
        "title": "Answer Birch Instruments' expense dispute",
        "brief": PLAIN.brief,
        "deliverables": list(PLAIN.deliverables),
        "success_criteria": [{"requirement": PLAIN.requirements[0], "observable": "x", "method": "y"}],
    }
    (folder / "tasks" / "acme_o2").mkdir()
    (folder / "tasks" / "acme_o2" / "workflow.json").write_text(json.dumps(clean))
    models = Models([RewrittenBriefs(tasks=[PLAIN])])
    result = rewrite_briefs(root, folder, models=models)
    assert (result["tasks"], result["kept"]) == (1, 1)
    assert "acme_o1" in models.prompts[0] and "acme_o2" not in models.prompts[0]
    kept = json.loads((folder / "tasks" / "acme_o2" / "workflow.json").read_text())
    assert kept["brief"] == clean["brief"] and kept["title"] == clean["title"]
    # The driver treats a missing plain_brief as unprepared work, so a kept task still
    # records one, with its own words as the text to restore.
    assert kept["plain_brief"]["kept"] and kept["plain_brief"]["before"]["brief"] == clean["brief"]
    # hub_vm refuses a task with no public brief, so a kept task still gets one.
    assignment = json.loads((folder / "tasks" / "acme_o2" / "assignment.json").read_text())
    assert (assignment["brief"], assignment["title"]) == (clean["brief"], clean["title"])
    assert json.loads((folder / "tasks" / "_plain_brief" / "REWRITE.json").read_text())["kept"] == ["acme_o2"]


def test_a_company_with_nothing_failing_calls_no_model(tmp_path):
    """Every call is another chance to drop a number; a clean company does not need one."""
    root, folder = make_company(tmp_path)
    path = folder / "tasks" / "acme_o1" / "workflow.json"
    write_clean = {
        **OLD,
        "title": "Answer Birch Instruments' expense dispute",
        "brief": PLAIN.brief,
        "deliverables": list(PLAIN.deliverables),
        "success_criteria": [{"requirement": PLAIN.requirements[0], "observable": "x", "method": "y"}],
    }
    path.write_text(json.dumps(write_clean))
    # An empty answer list raises IndexError if anything asks the model.
    result = rewrite_briefs(root, folder, models=Models([]))
    assert (result["tasks"], result["kept"], result["attempts"]) == (0, 1, 0)
    marker = json.loads((folder / "tasks" / "_plain_brief" / "REWRITE.json").read_text())
    assert marker["receipt"] is None and marker["register"] == {}
    assert json.loads(path.read_text())["brief"] == PLAIN.brief
    assert rewrite_briefs(root, folder, models=Models([]))["skipped"]


def test_rewrite_briefs_feeds_problems_back_then_gives_up(tmp_path):
    root, folder = make_company(tmp_path)
    lossy = PLAIN.model_copy(update={"brief": "Birch Instruments is disputing charges. Answer them soon."})
    models = Models([RewrittenBriefs(tasks=[lossy]), RewrittenBriefs(tasks=[PLAIN])])
    assert rewrite_briefs(root, folder, models=models)["attempts"] == 2
    assert "revision_feedback" in models.prompts[1]
    root2, folder2 = make_company(tmp_path / "b")
    with pytest.raises(ModelOutputInvalid):
        rewrite_briefs(root2, folder2, models=Models([RewrittenBriefs(tasks=[lossy])] * 3))


def test_each_requirement_keeps_its_own_facts():
    from company_envs.world.brief_rewrite import check_rewrite

    class New:
        title = "Plan the week"
        brief = "Plan nursing for September 8 to 14 within the $510 allowance."
        deliverables = ("A schedule",)
        requirements = ("The schedule stays within the allowance.", "Visits use 09:00 slots.")

    old = {
        "id": "t",
        "brief": "Plan nursing for September 8 to 14 within the $510 allowance.",
        "deliverables": ["A schedule"],
        "success_criteria": [
            {"requirement": "The schedule stays within the $510 allowance."},
            {"requirement": "Visits use 09:00 slots."},
        ],
    }
    problems = check_rewrite(old, New())
    assert any(p.startswith("requirement 1 lost its numbers or dates") for p in problems)
    assert not any(p.startswith("requirement 2") for p in problems)


def test_the_rewrite_marker_cannot_record_a_pass_for_a_rewrite_that_did_nothing(tmp_path):
    """REWRITE.json was the other writer scoped for migration, and it is the same inverted conclusion
    as ASSIGN.json: the empty case is already unreachable.

    A company with no tasks raises. `attempts: 0` is reached only when every brief already read
    plainly -- and then `kept` names all of them, so `attempts: 0` with an empty `kept`, the one shape
    that would be an empty run wearing a pass, cannot be written. Measured 2026-09-10: 0 of the 99
    REWRITE.json on disk even hold `attempts: 0`, so the legitimate version has never occurred
    either. The outcome is named and pinned here so the guarantee is checkable rather than implied by
    a raise and a list comprehension thirty lines apart.
    """
    root, folder = make_company(tmp_path)
    for path in folder.glob("tasks/*/workflow.json"):
        path.unlink()
    with pytest.raises(ValueError, match="no tasks to rewrite"):
        rewrite_briefs(root, folder, models=Models([]))
    assert not (folder / "tasks" / "_plain_brief" / "REWRITE.json").exists(), "no marker, so no verdict"


def test_a_rewrite_already_done_is_a_pass_and_says_so_rather_than_tasks_zero(tmp_path):
    """`{"tasks": 0, "skipped": "already rewritten"}` is the shape a rewrite that achieved nothing
    would also have had. It is a pass: the rewrite the marker names is on disk."""
    root, folder = make_company(tmp_path)
    rewrite_briefs(root, folder, models=Models([RewrittenBriefs(tasks=[PLAIN])]))
    again = rewrite_briefs(root, folder, models=Models([]))
    assert again["outcome"] == "passed" and again["ok"] is True
    assert again["tasks"] == 0 and again["skipped"] == "already rewritten"
    marker = json.loads((folder / "tasks" / "_plain_brief" / "REWRITE.json").read_text())
    assert marker["outcome"] == "passed" and marker["attempts"] == 1
