"""Independent review ballots, using fixed verdicts and no model service."""

import json
from copy import deepcopy

import pytest
from test_contracts import verdict

from company_envs.review import merge_votes, review, review_payload
from company_envs.storage import bound_review, digest


class Ballots:
    def __init__(self, readings, votes=None):
        self.readings = readings
        self.config = {"generation": {} if votes is None else {"review_votes": votes}}
        self.calls = {}

    def call(self, job, prompt, response_type, *, avoid, validate):
        packet = json.loads(prompt.rsplit("\n", 1)[1])
        index = packet.get("vote", 0)
        self.calls[index] = (job, prompt, response_type, avoid)
        result = deepcopy(self.readings[index])
        validate(result)
        return result, {
            "model": "codex/reviewer",
            "session_id": f"review-{index}",
            "call_id": f"call-{index}",
            "status": "complete",
            "prompt_hash": digest(prompt),
        }


def run(models, company, workflows):
    return review(models, "Review these facts", company, workflows, [], [], {}, {"codex/author"})


def reading(workflow, outcome, reasons=()):
    result = verdict(workflow, verdict=outcome, reasons=list(reasons))
    result.company_verdict = outcome
    result.company_reasons = list(reasons)
    return result


def test_two_of_three_accept_preserves_each_ballot(company, workflow):
    readings = [reading(workflow, "revise", ["Clarify the deadline"]), verdict(workflow), verdict(workflow)]
    models = Ballots(readings, 3)
    result = run(models, company, [workflow])
    assert result["review"] == readings[1].model_dump()
    receipt = result["receipt"]
    assert receipt["votes"] == 3 and receipt["accepts"] == 2
    assert [b["verdict"] for b in receipt["ballots"]] == [r.model_dump() for r in readings]
    packets = [json.loads(models.calls[i][1].split("\n", 1)[1]) for i in range(3)]
    assert [p.pop("vote") for p in packets] == [0, 1, 2]
    assert packets[0] == packets[1] == packets[2]
    assert len({b["receipt"]["prompt_hash"] for b in receipt["ballots"]}) == 3


def test_one_of_three_accept_merges_dissent_by_target_and_issue_prefix(company, workflow):
    prefix = "The shipping deadline needs an explicit time zone. " * 2
    readings = [
        verdict(workflow),
        reading(workflow, "revise", [prefix + "First", "Name the owner"]),
        reading(workflow, "reject", [prefix + "Second", "State the amount"]),
    ]
    second = workflow.model_copy(update={"id": "second"})
    for r in readings:
        r.tasks.append(r.tasks[0].model_copy(update={"workflow_id": second.id}))
    before = deepcopy(readings)
    result = run(Ballots(readings, 3), company, [workflow, second])
    merged = result["review"]
    assert merged["company_verdict"] == "revise"
    expected = [prefix + "First", "Name the owner", "State the amount"]
    assert merged["company_reasons"] == expected
    assert [t["reasons"] for t in merged["tasks"]] == [expected, expected]
    assert all(t["verdict"] == "revise" for t in merged["tasks"])
    assert readings == before


@pytest.mark.parametrize("votes", [None, 1])
def test_single_vote_keeps_original_prompt_receipt_and_result_bytes(company, workflow, votes):
    original = reading(workflow, "reject", ["No captured source"])
    models = Ballots([original], votes)
    result = run(models, company, [workflow])
    packet = review_payload(company.model_dump(), [workflow.model_dump()], [], [], {})
    prompt = "Review these facts\n" + json.dumps(packet, ensure_ascii=False)
    expected = {
        "review": original.model_dump(),
        "receipt": {
            "model": "codex/reviewer",
            "session_id": "review-0",
            "call_id": "call-0",
            "status": "complete",
            "prompt_hash": digest(prompt),
        },
        "input_hash": bound_review(
            company.model_dump(),
            [workflow.model_dump()],
            [],
            "Review these facts",
            {"codex/author"},
            "different_model",
            None,
        ),
        "review_policy": "different_model",
        "separate_witness": False,
        "authors": ["codex/author"],
        "neighbors": {},
        "source_excerpts": [],
    }
    assert models.calls[0][1] == prompt
    assert len(models.calls) == 1
    assert json.dumps(result, ensure_ascii=False) == json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize(
    "outcomes, expected",
    [
        (["reject", "accept", "reject"], "reject"),
        (["accept", "revise"], "revise"),
        (["revise", "reject", "revise"], "revise"),
    ],
)
def test_reject_majority_and_ties(workflow, outcomes, expected):
    result = merge_votes([reading(workflow, v, [] if v == "accept" else [v]) for v in outcomes])
    assert result.company_verdict == result.tasks[0].verdict == expected


def test_company_and_tasks_are_decided_separately(company, workflow):
    readings = [verdict(workflow) for _ in range(3)]
    readings[0].tasks[0].verdict = readings[1].tasks[0].verdict = "revise"
    readings[0].tasks[0].reasons = readings[1].tasks[0].reasons = ["Clarify the price"]
    result = run(Ballots(readings, 3), company, [workflow])["review"]
    assert result["company_verdict"] == "accept"
    assert result["tasks"][0]["verdict"] == "revise"


def test_every_ballot_must_pass_author_separation(company, workflow):
    class ReusedAuthor(Ballots):
        def call(self, *args, **kwargs):
            result, receipt = super().call(*args, **kwargs)
            if receipt["call_id"] == "call-2":
                receipt["model"] = "codex/author"
            return result, receipt

    with pytest.raises(ValueError, match="also authored"):
        run(ReusedAuthor([verdict(workflow)] * 3, 3), company, [workflow])


@pytest.mark.parametrize("votes", [1, 3])
def test_saved_receipts_verify_and_detect_tampering(company, workflow, votes):
    from company_envs.review import validate_review_receipt

    models = Ballots([verdict(workflow)] * votes, votes)
    result = run(models, company, [workflow])
    payload = review_payload(company.model_dump(), [workflow.model_dump()], [], [], {})

    def verify(value, count=votes):
        validate_review_receipt(
            value,
            payload,
            "Review these facts",
            count,
            [workflow],
            "different_model",
            [{"model": "codex/author"}],
        )

    verify(result)
    with pytest.raises(ValueError, match="vote count"):
        verify(result, votes + 1)
    changed = deepcopy(result)
    call = changed["receipt"] if votes == 1 else changed["receipt"]["ballots"][-1]["receipt"]
    call["prompt_hash"] = "changed"
    with pytest.raises(ValueError, match="review prompt"):
        verify(changed)
    call["model"] = "codex/author"
    with pytest.raises(ValueError, match="also authored"):
        verify(changed)
    if votes > 1:
        changed = deepcopy(result)
        changed["receipt"]["ballots"].pop()
        with pytest.raises(ValueError, match="every ballot"):
            verify(changed)
        changed = deepcopy(result)
        changed["receipt"]["accepts"] = 0
        with pytest.raises(ValueError, match="accept count"):
            verify(changed)
        changed = deepcopy(result)
        changed["review"]["company_verdict"] = "reject"
        changed["review"]["company_reasons"] = ["Forged rejection"]
        with pytest.raises(ValueError, match="verdict differs"):
            verify(changed)


@pytest.mark.parametrize(
    "outcomes,expected",
    [
        (["revise", "modify_brief", "accept"], "modify_brief"),
        (["accept", "modify_brief", "modify_brief"], "modify_brief"),
        (["modify_brief", "reject", "reject"], "reject"),
        (["modify_brief", "revise"], "revise"),
    ],
)
def test_brief_fixes_vote_with_accepts(company, workflow, outcomes, expected):
    from test_critic_rewrite import FIX, brief_verdict

    from company_envs.review import validate_review_receipt

    readings = [
        brief_verdict(workflow)
        if outcome == "modify_brief"
        else verdict(workflow, verdict=outcome, reasons=["Finding"])
        for outcome in outcomes
    ]
    for index, ballot in enumerate(readings):
        if ballot.tasks[0].verdict == "modify_brief":
            ballot.tasks[0].brief_fix = FIX + f" Call this draft {index}."
    originals = deepcopy(readings)
    result = run(Ballots(readings, len(readings)), company, [workflow])
    task = result["review"]["tasks"][0]
    assert task["verdict"] == expected
    if expected == "modify_brief":
        first_fix = next(b.tasks[0] for b in readings if b.tasks[0].verdict == "modify_brief")
        assert task == first_fix.model_dump()
    else:
        assert "brief_fix" not in task
    assert readings == originals
    validate_review_receipt(
        result,
        review_payload(company.model_dump(), [workflow.model_dump()], [], [], {}),
        "Review these facts",
        len(readings),
        [workflow],
        "different_model",
        [{"model": "codex/author"}],
    )
