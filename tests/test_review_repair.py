"""review-repair: a rejected world's blocking findings go back to the apps that own the cited
records, the reviewers read the repaired world again, and the receipt keeps one world to one
attempt. Fake author and fake reviewer; no model service and no re-seeding."""

import json

import pytest
from test_bulk import EMAIL, STUB_TEMPLATE, _seeded_company, spec

from company_envs.storage import read, write
from company_envs.world.seed_calls import AppStateResult, WorkerMaterial
from company_envs.world.world_repair import repair_review, stale_verdict, verdict_coverage
from company_envs.world.world_review import (
    MaterialsOnly,
    ReviewFinding,
    WorldReview,
    review_inputs,
)

DRIFTED = {
    **EMAIL,
    "id": "mail-42",
    "subject": "Harbor Supply August statement",
    "body": (
        "Hi Jo, the August statement for Harbor Supply is attached. It closes at USD 1,200 "
        "outstanding, which is the figure the vendor record contradicts; the credits from the "
        "July delivery are applied on the next page and nothing else is open. Ana"
    ),
}
FIXED = {**DRIFTED, "body": DRIFTED["body"].replace("USD 1,200", "USD 9,400")}
# A body the checker reads as a seeding instruction left in the record: an error naming gmail_mock.
BAD_BODY = {**EMAIL, "id": "e2", "body": "expand these literally for i=1..600 to fill the inbox"}


def finding(target, issue, evidence, severity="error"):
    return {"target": target, "severity": severity, "issue": issue, "evidence": evidence}


DRIFT_FINDING = finding(
    "gmail_mock",
    "The August statement email states an outstanding balance the vendor record contradicts.",
    "/app_states/gmail_mock/emails/mail-42 says USD 1,200; the canonical vendor says USD 9,400.",
)


def ballot(verdict, findings=()):
    return WorldReview(
        verdict=verdict,
        summary=f"{verdict} reading",
        findings=[ReviewFinding.model_validate(f) for f in findings],
    )


class Models:
    """Answers app_state repairs and world_review readings; records every payload it was sent."""

    def __init__(self, repaired=None, ballots=(), app_id="gmail_mock"):
        self.repaired, self.ballots, self.app_id = repaired, list(ballots), app_id
        self.payloads = []

    def call(self, job, prompt, response_type, **kwargs):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        if response_type is WorldReview:
            assert job == "world_review" and payload["call"] == "world_review"
            return self.ballots.pop(0), {"model": "fake", "call_id": f"review-{payload['round']}"}
        assert job == "world_states" and response_type is AppStateResult
        assert payload["call"] == "app_state", "a repair never re-seeds the world core"
        previous = json.loads(payload["previous_state_json"])
        fixed = self.repaired(payload, previous) if self.repaired else previous
        return AppStateResult(app_id=self.app_id, rationale="repaired", state_json=json.dumps(fixed)), {
            "model": "fake",
            "call_id": "repair",
        }

    def calls(self):
        return [p["call"] for p in self.payloads]


@pytest.fixture
def rejected(tmp_path):
    """A seeded company the reviewers sent back, with its REVIEW.json and SEED.json as seeding
    leaves them; the caller fills in the final round's findings."""
    root, folder = _seeded_company(tmp_path, [EMAIL, DRIFTED])
    skill = root / ".agents/skills/company-world-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: company-world-review\n---\nRead the world.\n")
    world = read(folder / "world/world.json")
    world["vendors"][0]["outstanding"] = "USD 9,400"
    # A canonical record no app serves: nothing the app authors can be asked to rewrite.
    world["history"] = [{"id": "hist-0725", "what": "July delivery accepted", "date": "2025-07-14"}]
    write(folder / "world/world.json", world)

    def reject(findings):
        write(
            folder / "world/REVIEW.json",
            {
                "rounds": [
                    {
                        "round": 0,
                        "verdict": {"verdict": "revise", "summary": "one reading", "findings": findings},
                        "receipt": {"votes": 1, "cast": 1},
                    }
                ],
                "final_verdict": "revise",
            },
        )
        seed = read(folder / "world/SEED.json")
        seed.update({"status": "seeded_review_failed", "review": "revise", "review_verdict": "revise"})
        write(folder / "world/SEED.json", seed)
        return root, folder

    return reject


def test_a_repaired_world_is_accepted_and_the_seed_verdict_follows(rejected):
    root, folder = rejected([DRIFT_FINDING, finding("gmail_mock", "A stray comma.", "e1", "warning")])

    def repaired(payload, previous):
        # The finding reaches the author as its own feedback, with the previous state to correct.
        feedback = payload["revision_feedback"]
        assert feedback[0]["issue"] == DRIFT_FINDING["issue"]
        # The worklist, named the way gym-anything names one: these records and no others.
        assert feedback[-1]["evidence"] == "mail-42"
        assert feedback[-1]["issue"].startswith("the findings above name these records")
        assert previous["emails"] == [EMAIL, DRIFTED], "the records to correct, not a fresh app"
        return {**previous, "emails": [EMAIL, FIXED]}

    models = Models(repaired=repaired, ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert report["ok"] and report["verdict"] == "accept" and report["rounds"] == 1
    assert report["rewritten"] == {"gmail_mock": 1} and report["unrepaired"] == []
    assert models.calls() == ["app_state", "world_review"]
    assert read(folder / "world/gmail_mock.state.json")["emails"][1] == FIXED
    seed = read(folder / "world/SEED.json")
    assert seed["review_verdict"] == "accept" and seed["review"] == "accept"
    assert seed["status"] == "seeded_reviewed" and seed["mechanical_ok"] is True
    # This file is the *second* writer of SEED.json, and the shared outcome moves with the status it
    # is derived from: both go through state_seed.seed_outcome, so a verdict flipped here cannot
    # leave a refusal standing in the receipt beside a status that says accepted.
    assert seed["outcome"] == "passed" and seed["ok"] is True
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert receipt["ok"] and receipt["before"] == {"verdict": "revise", "blocking_findings": 1}
    assert [a["target"] for a in receipt["addressed"]] == ["gmail_mock"]
    assert receipt["rewritten"] == {"gmail_mock": {"records": 1, "ids": ["mail-42"]}}
    assert receipt["review_rounds"][0]["repaired"] == ["gmail_mock"]
    assert receipt["review_rounds"][0]["verdict"]["verdict"] == "accept"
    assert receipt["receipts"]["apps"]["gmail_mock"][0]["call_id"] == "repair"
    assert "world/REVIEW-REPAIR.json" in read(folder / "MANIFEST.json")["hashes"]
    assert read(folder / "MANIFEST.json")["stages"]["stage2_world"] == "seeded_reviewed"


def test_a_repair_the_reviewers_still_reject_leaves_the_world_rejected(rejected):
    root, folder = rejected([DRIFT_FINDING])
    tries = []

    def repaired(_payload, previous):
        # Each round lands a real change: a round that changes nothing is a failed author now and
        # stops the loop, which the next test measures.
        tries.append(len(tries))
        return {**previous, "emails": [EMAIL, {**FIXED, "body": f"{FIXED['body']} ({len(tries)})"}]}

    models = Models(
        repaired=repaired, ballots=[ballot("revise", [DRIFT_FINDING]), ballot("revise", [DRIFT_FINDING])]
    )
    report = repair_review(root, folder, models=models)

    assert not report["ok"] and report["verdict"] == "revise" and report["rounds"] == 2
    assert models.calls() == ["app_state", "world_review", "app_state", "world_review"]
    assert [p["round"] for p in models.payloads if p["call"] == "world_review"] == [1, 2]
    # The loop knows what it was sent to repair, so a re-reading of the same slip is not a new one.
    rounds = read(folder / "world/REVIEW-REPAIR.json")["review_rounds"]
    assert [(r["receipt"]["new_issues"], r["receipt"]["persisting_issues"]) for r in rounds] == [
        (0, 1),
        (0, 1),
    ]
    assert report["unrepaired"] == [
        {
            "target": "gmail_mock",
            "issue": DRIFT_FINDING["issue"],
            "reason": "still reported after 2 round(s)",
        }
    ]
    seed = read(folder / "world/SEED.json")
    assert seed["review_verdict"] == "revise" and seed["status"] == "seeded_review_failed"
    assert read(folder / "world/REVIEW-REPAIR.json")["ok"] is False


def test_findings_nothing_owns_are_recorded_with_a_reason_and_cost_no_call(rejected):
    root, folder = rejected(
        [
            finding("materials", "Jo's desktop note repeats the brief.", "notes/onboarding.md"),
            finding("world", "The delivery history rehearses one shape.", "the history array"),
            finding("world", "The July delivery has no counterpart anywhere.", "history hist-0725"),
        ]
    )
    models = Models(ballots=[])
    report = repair_review(root, folder, models=models)

    assert not report["ok"] and report["rounds"] == 0 and models.payloads == []
    assert report["verdict"] == "revise" and report["rewritten"] == {}
    assert [(f["target"], f["reason"]) for f in report["unrepaired"]] == [
        ("materials", "a worker's desktop file, and this world ships none"),
        ("world", "cites no record of this world, only a collection or a judgement"),
        ("world", "cites only records no app serves"),
    ]
    assert read(folder / "world/SEED.json")["review_verdict"] == "revise"
    assert read(folder / "world/REVIEW-REPAIR.json")["rounds"] == 0


def test_the_receipt_is_written_even_when_the_author_fails(rejected):
    root, folder = rejected([DRIFT_FINDING])
    models = Dropped(ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert not report["ok"] and "dropped the connection" in report["error"]
    assert "world_review" not in models.calls(), "the reviewer is not paid for a repair that never landed"
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert "dropped the connection" in receipt["error"] and receipt["ok"] is False
    assert receipt["unrepaired"][0]["reason"] == "still reported after 1 round(s)"


def test_a_folder_missing_an_artifact_still_leaves_the_receipt(rejected):
    # The receipt is what keeps the driver to one attempt, so nothing may stop it being written.
    root, folder = rejected([DRIFT_FINDING])
    (folder / "world/identities.json").unlink()
    report = repair_review(root, folder, models=Models())

    assert not report["ok"] and "identities.json" in report["error"]
    assert read(folder / "world/REVIEW-REPAIR.json")["ok"] is False
    assert read(folder / "world/SEED.json")["review_verdict"] == "revise"


def test_a_world_with_no_review_is_left_alone_with_a_receipt(tmp_path):
    root, folder = _seeded_company(tmp_path, [EMAIL])
    models = Models()
    report = repair_review(root, folder, models=models)

    assert not report["ok"] and report["rounds"] == 0 and report["verdict"] is None
    assert models.payloads == [] and report["unrepaired"] == []
    assert read(folder / "world/REVIEW-REPAIR.json")["before"]["blocking_findings"] == 0


class Unusable(Models):
    """A model whose reply for one app is not the object the author can read."""

    def call(self, job, prompt, response_type, **kwargs):
        if response_type is not WorldReview:
            self.payloads.append(json.loads(prompt.rsplit("\n", 1)[1]))
            # A JSON array where the author needs an object: exactly what the provider returned.
            return AppStateResult(app_id=self.app_id, rationale="x", state_json="[]"), {"model": "fake"}
        return super().call(job, prompt, response_type, **kwargs)


class Dropped(Models):
    """A provider fault the author cannot salvage: the call itself never returns a reply."""

    def call(self, job, prompt, response_type, **kwargs):
        if response_type is not WorldReview:
            raise RuntimeError("the provider dropped the connection")
        return super().call(job, prompt, response_type, **kwargs)


def test_a_round_where_every_author_fails_does_not_pay_the_reviewers(rejected):
    """The real failure seen live: the model answered with a list, so no app could be rewritten.
    The author salvages a group it cannot write and hands back what was there, so the round ends
    with nothing changed -- which is the other half of this defect and costs the same reading."""
    root, folder = rejected([DRIFT_FINDING])
    models = Unusable(ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert report["ok"] is False and "gmail_mock" in report["error"]
    assert "world_review" not in models.calls(), "nothing landed, so the world is not re-read"
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    # The author's receipts and the reason an app failed are separate: writing the reason into the
    # author's own receipt list is what killed three worlds with 'str' has no attribute 'append'.
    assert receipt["ok"] is False and receipt["receipts"]["failed"]["gmail_mock"]
    assert isinstance(receipt["receipts"]["apps"].get("gmail_mock", []), list)


def test_a_repair_that_changed_no_record_is_a_failed_author_not_a_repair(rejected):
    """``narrow_repair`` reduced the author's output to zero changed records, the reviewer duly
    reported the same defect again, and the driver read that as the loop diverging. Measured over
    the 35 recorded repair receipts: 25 of the 98 app repairs changed no record at all (26%, in 16
    companies -- {'contractbook_mock': 0}, {'google_sheets_mock': 0}, {'google_docs_mock': 0,
    'salesforce_mock': 0, 'slack_mock': 0}), 29 of the 52 reader passes were paid inside a receipt
    holding one, and 5 of the 25 repaired-and-rejected worlds are terminal on a round whose repair
    changed nothing in an app the next round's errors go on to name. A narrowing to zero is a failed
    author: it is recorded as one, it is not counted as a repair, and it is not read again."""
    root, folder = rejected([DRIFT_FINDING])
    # The author answers with exactly what it was given: a repair that repaired nothing.
    models = Models(repaired=lambda _payload, previous: previous, ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert report["rounds"] == 1 and report["ok"] is False and report["verdict"] == "revise"
    assert models.calls() == ["app_state"], "no reader pass for a world that did not change"
    assert report["rewritten"] == {}, "nothing was repaired, so nothing is reported as repaired"
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert receipt["receipts"]["failed"] == {
        "gmail_mock": "the author returned no change to any record the findings named"
    }
    assert receipt["review_rounds"] == [], "the round never reached a reading"
    assert receipt["unrepaired"][0]["reason"] == "still reported after 1 round(s)"


def test_a_collection_too_big_for_one_prompt_is_the_one_that_gets_sampled():
    """``too_large_to_send`` used to mean "never sent"; it now means "sent as a sample".

    The predicate itself is unchanged and still the right line: measured over the 406 app layers on
    disk, 84 of them in 41 of the 60 companies are past it, google_drive_mock /items and gmail_mock
    /emails at 1.7-4.0 million characters. What changed is what the repair does with the answer.
    """
    from company_envs.world.world_repair import PROMPT_LIMIT, largest_collection, too_large_to_send

    small = {"emails": [{"id": "m1", "body": "short"}]}
    assert not too_large_to_send(small) and largest_collection(small) < 200
    huge = {"items": [{"id": f"f{i}", "name": "x" * 400} for i in range(4000)]}
    assert largest_collection(huge) > PROMPT_LIMIT and too_large_to_send(huge)
    assert largest_collection({}) == 0 and not too_large_to_send({})


def test_a_long_failure_keeps_the_end_where_the_reason_is():
    """A failed subprocess renders as its whole command line, then the reason. Keep both ends."""
    from company_envs.world.world_repair import both_ends

    message = "Command '[" + "'--flag', " * 200 + "]' timed out after 600 seconds"
    trimmed = both_ends(message, 200)
    assert len(trimmed) <= 200
    assert trimmed.startswith("Command '[") and trimmed.endswith("timed out after 600 seconds")
    assert both_ends("short", 200) == "short"


def test_a_world_that_is_already_accepted_is_never_rewritten(rejected):
    """Seen live: a repair rewrote four apps of a world another pass had recovered, and ended on
    'revise' while the world's own verdict still said accept."""
    root, folder = rejected([DRIFT_FINDING])
    seed = read(folder / "world/SEED.json")
    seed["review_verdict"] = "accept"
    write(folder / "world/SEED.json", seed)
    models = Models(ballots=[ballot("revise")])

    report = repair_review(root, folder, models=models)

    assert report["ok"] is True and report["verdict"] == "accept"
    assert models.calls() == [], "no author and no reviewer is paid to redo settled work"
    assert read(folder / "world/REVIEW-REPAIR.json")["note"] == "already accepted; nothing repaired"


def test_a_repair_that_rewrites_a_record_nobody_cited_has_it_put_back(rejected):
    """23,522 records were rewritten against 578 cited, 41 for every one named. Each of those is a
    record the reviewers had already read, re-rolled for nothing, which is how a repair round ends
    with more findings than it started with. CUA-Gym asks for this in prose and checks nothing
    after; gym-anything hands over a named worklist and leaves the rest to the reader."""
    root, folder = rejected([DRIFT_FINDING])
    churn = {**EMAIL, "body": "an unrelated rewrite nobody asked for"}

    def repaired(_payload, previous):
        return {**previous, "emails": [churn, FIXED]}

    report = repair_review(root, folder, models=Models(repaired=repaired, ballots=[ballot("accept")]))

    assert report["ok"] and report["rewritten"] == {"gmail_mock": 1}
    assert read(folder / "world/gmail_mock.state.json")["emails"] == [EMAIL, FIXED]
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert receipt["narrowed"] == {"1": {"gmail_mock": {"put_back": 1, "collections_opened": []}}}
    assert receipt["rewritten"] == {"gmail_mock": {"records": 1, "ids": ["mail-42"]}}


def test_the_reason_an_app_failed_never_lands_in_the_author_receipt_list(rejected):
    """Three worlds died on "AttributeError: 'str' object has no attribute 'append'", and the
    receipt the driver read said rounds 0, unrepaired []. ``folder_author`` appends each call's
    receipt to receipts["apps"][app_id]; the reason an app was skipped or failed was written to
    that same key, so the next round's author found a string where its list had been."""
    root, folder = rejected([DRIFT_FINDING])
    report = repair_review(root, folder, models=Dropped(ballots=[ballot("accept")]))

    receipts = read(folder / "world/REVIEW-REPAIR.json")["receipts"]
    assert "dropped the connection" in receipts["failed"]["gmail_mock"]
    assert all(isinstance(value, list) for value in receipts["apps"].values())
    assert isinstance(receipts["apps"].get("gmail_mock", []), list), "what folder_author appends to"
    assert report["ok"] is False and "dropped the connection" in report["error"]


def test_a_crash_outside_the_author_still_names_the_outstanding_findings(rejected):
    """An empty unrepaired list on a crashed run is indistinguishable from a clean one: an
    AttributeError escaped a guard that named three exception types, and the caller's fallback
    receipt -- rounds 0, unrepaired [] -- cannot know what was left outstanding."""
    root, folder = rejected([DRIFT_FINDING])

    class BreaksOnReview(Models):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is WorldReview:
                raise AttributeError("'str' object has no attribute 'append'")
            return super().call(job, prompt, response_type, **kwargs)

    report = repair_review(
        root, folder, models=BreaksOnReview(repaired=lambda _p, previous: {**previous, "emails": [FIXED]})
    )

    assert report["ok"] is False and report["rounds"] == 1
    assert "AttributeError: 'str' object has no attribute 'append'" in report["error"]
    assert report["unrepaired"] == [
        {
            "target": "gmail_mock",
            "issue": DRIFT_FINDING["issue"],
            "reason": "still reported after 1 round(s)",
        }
    ]
    assert read(folder / "world/REVIEW-REPAIR.json")["unrepaired"] == report["unrepaired"]


def test_a_verdict_cast_on_a_tenth_of_the_world_is_partial_and_a_rewrite_makes_it_wrong(rejected):
    """The review is the last step of seeding; ``dedupe-names`` and ``add-bulk`` run after it and the
    bulk layer is 88-90% of every mailbox, so across the 60 seeded folders the verdicts read 124,708
    records of the 331,577 that ship (37.6%, worst world 8.8%). Growth is not staleness: counting it
    as staleness fires the flag on all 42 of the 60 worlds that have a bulk layer, and a flag always
    on is a flag nothing acts on. The share is ``coverage``; what makes a verdict *wrong* is a record
    it read going missing or being rewritten where it read it. A verdict that recorded no inputs at
    all is unknown, not stale -- refusing all sixty would stop the fleet over a fact none of them
    can establish."""
    root, folder = rejected([DRIFT_FINDING])
    assert stale_verdict(folder) == [], "the fixture's verdict recorded no inputs"
    assert verdict_coverage(folder) is None

    def record_inputs(states):
        review = read(folder / "world/REVIEW.json")
        review["rounds"][0]["inputs"] = review_inputs(read(folder / "world/world.json"), states, [])
        write(folder / "world/REVIEW.json", review)

    # A reading of part of what now ships: partial, and the share says how partial.
    record_inputs({"gmail_mock": {"users": [{"id": "u1"}], "emails": [EMAIL]}})
    assert stale_verdict(folder) == []
    assert verdict_coverage(folder) == 0.6667, "two records read of the three on disk"

    # The same records, rewritten under the verdict: that reading is no longer true of anything.
    on_disk = read(folder / "world/gmail_mock.state.json")
    record_inputs({"gmail_mock": on_disk})
    assert stale_verdict(folder) == [] and verdict_coverage(folder) == 1.0
    renamed = {**on_disk, "emails": [{**on_disk["emails"][0], "to": "Dana Reed"}, *on_disk["emails"][1:]]}
    write(folder / "world/gmail_mock.state.json", renamed)
    assert stale_verdict(folder) == [
        "gmail_mock was rewritten after the verdict (the same 3 records, new content)"
    ]

    report = repair_review(
        root,
        folder,
        models=Models(
            repaired=lambda _p, previous: {**previous, "emails": [FIXED]}, ballots=[ballot("accept")]
        ),
    )
    # ``stale`` is what the verdict this step *started from* no longer covers, so the rewrite the
    # test just made is named there; the repair's own reading then covers what is on disk.
    assert report["coverage"] == 1.0
    assert report["stale"] == ["gmail_mock was rewritten after the verdict (the same 3 records, new content)"]
    # REVIEW.json keeps its readings under "rounds"; REVIEW-REPAIR.json's "rounds" is a count, and
    # reading it as a list of readings raised TypeError on 29 of the 31 folders that have one.
    assert read(folder / "world/REVIEW-REPAIR.json")["rounds"] == 1
    assert stale_verdict(folder) == [], "the repair's own reading covers what is on disk now"


def test_a_finding_on_a_desktop_file_is_repaired_here_instead_of_blocking_forever(rejected):
    """``merge_ballots`` lets a ``materials`` finding be decisive and ``repair_review`` sent findings
    only to the apps that own the cited records, so a whole blocking class had no author by
    construction. Measured over the 35 recorded repair receipts: 25 material findings across 8
    worlds left unrepaired, 10 of them refused at entry as "a worker's desktop file, not an app
    record", and hammond-power-solutions terminal with all three of its unrepaired findings naming
    materials (fairfax-county 4 of its 6). The choice is to give it the owner the seeding loop has
    always had -- ``repair_materials`` -- rather than drop material findings from the blocking set,
    because a defect in the file a task hands the worker is a real defect."""
    root, folder = rejected([finding("materials", "Jo's note contradicts the statement.", "notes/brief.md")])
    note = folder / "world/materials/w1/notes/brief.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("The Harbor Supply balance is USD 1,200.\n")

    class WithMaterials(Models):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is MaterialsOnly:
                payload = json.loads(prompt.rsplit("\n", 1)[1])
                self.payloads.append(payload)
                assert payload["call"] == "materials_repair"
                assert payload["feedback"][0]["target"] == "materials"
                return MaterialsOnly(
                    materials=[WorkerMaterial(worker_id="w1", path="notes/brief.md", content="USD 9,400.\n")]
                ), {"model": "fake", "call_id": "materials"}
            return super().call(job, prompt, response_type, **kwargs)

    models = WithMaterials(ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert report["ok"] and report["verdict"] == "accept"
    assert report["materials_rewritten"] == ["w1/notes/brief.md"]
    assert note.read_text() == "USD 9,400.\n"
    assert models.calls() == ["materials_repair", "world_review"], "no app author for a desktop file"
    assert report["unrepaired"] == []
    assert read(folder / "world/REVIEW-REPAIR.json")["review_rounds"][0]["materials"] == ["w1/notes/brief.md"]


def test_a_materials_repair_that_changed_no_file_is_not_paid_a_reading(rejected):
    """``repair_materials`` must return every file it was given, so a repair that changed nothing
    comes back the same length as it went in and looks like a repair. The same rule as the app
    authors: a round that landed nothing is not read again."""
    root, folder = rejected([finding("materials", "Jo's note contradicts the statement.", "notes/brief.md")])
    note = folder / "world/materials/w1/notes/brief.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("The Harbor Supply balance is USD 1,200.\n")

    class UnchangedMaterials(Models):
        def call(self, job, prompt, response_type, **kwargs):
            if response_type is MaterialsOnly:
                self.payloads.append(json.loads(prompt.rsplit("\n", 1)[1]))
                return MaterialsOnly(
                    materials=[
                        WorkerMaterial(
                            worker_id="w1",
                            path="notes/brief.md",
                            content="The Harbor Supply balance is USD 1,200.\n",
                        )
                    ]
                ), {"model": "fake", "call_id": "materials"}
            return super().call(job, prompt, response_type, **kwargs)

    models = UnchangedMaterials(ballots=[ballot("accept")])
    report = repair_review(root, folder, models=models)

    assert report["ok"] is False and report["materials_rewritten"] == []
    assert models.calls() == ["materials_repair"], "no reader pass for a world that did not change"
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert receipt["receipts"]["failed"] == {"materials": "the repair returned every desktop file unchanged"}
    assert note.read_text() == "The Harbor Supply balance is USD 1,200.\n"


def test_a_concurrent_repair_cannot_leave_a_rejected_world_stamped_accept(rejected):
    """Two repairs overlapped on ust: the first started 05:50 and wrote ``review_verdict: accept`` at
    09:44; the second started 08:55, so the entry guard read a rejected world, and finished at 12:38
    having rewritten five apps to a world the reviewers read as ``revise`` with two findings
    outstanding in the task's own decisive collection. ust's SEED.json says accept and its
    REVIEW-REPAIR.json says ``verdict: revise, ok: false, unrepaired: 2``, so the company passes the
    review gate on an accept that describes a world no longer on disk. The flag is read again after
    the repair and before the first byte of it lands, not only at entry."""
    root, folder = rejected([DRIFT_FINDING])
    state_before = read(folder / "world/gmail_mock.state.json")

    class AcceptsMidway(Models):
        """Another pass accepts this world while this one's author is running."""

        def call(self, job, prompt, response_type, **kwargs):
            seed = read(folder / "world/SEED.json")
            seed["review"] = seed["review_verdict"] = "accept"
            write(folder / "world/SEED.json", seed)
            return super().call(job, prompt, response_type, **kwargs)

    models = AcceptsMidway(repaired=lambda _p, previous: {**previous, "emails": [FIXED]}, ballots=[])
    report = repair_review(root, folder, models=models)

    assert report["ok"] is True and report["verdict"] == "accept"
    assert models.calls() == ["app_state"], "the repair it had already paid for, and no reader pass"
    assert read(folder / "world/gmail_mock.state.json") == state_before, "nothing written over it"
    receipt = read(folder / "world/REVIEW-REPAIR.json")
    assert receipt["note"] == (
        "another pass accepted this world while this one was repairing; nothing written"
    )
    assert receipt["rounds"] == 1
    assert receipt["review_rounds"] == [] and "error" not in receipt
    # The accepting pass owns SEED.json: this one adds none of its own bookkeeping to it.
    assert "review_repaired_at" not in read(folder / "world/SEED.json")


def test_a_layer_too_big_for_one_prompt_is_sampled_and_keeps_every_record(tmp_path):
    """``too_large_to_send`` named the app and spent no call on it, because the author had to be
    given a collection whole and hand it back whole. Measured over the 406 app layers on disk, that
    guard refuses **84 of them in 41 of the 60 companies** -- gmail_mock and google_drive_mock at
    1.7-4.0 million characters, macerich terminal on exactly this -- and those 84 are precisely the
    layers the sampled repair exists for, so the guard cancelled the fix. The author now samples the
    collection and applies a patch, so the app is sent; the receipt records that its prompt was a
    sample, and the records the sample left out are still there afterwards. The stub here answers
    with the sample it was shown, which is the worst honest answer a model can give: under the old
    contract that deleted 820 of 900 emails.
    """
    from test_bulk import _seeded_company

    from company_envs.world.state_seed import PROMPT_CEILING
    from company_envs.world.world_repair import repair_world

    # 900 ordinary emails, the shape of the real 84: no single record is large (the largest record
    # on disk anywhere is under 100,000 characters), the collection is.
    bulkish = [{**EMAIL, "id": f"e-{n}", "body": f"Jo, the {n} statement stands. " * 40} for n in range(900)]
    root, folder = _seeded_company(tmp_path, [EMAIL, BAD_BODY, *bulkish])
    before = read(folder / "world/gmail_mock.state.json")["emails"]
    assert len(json.dumps(before)) > 1_048_576 * 0.8, "the collection is past the whole-prompt limit"

    models = Models()
    report = repair_world(root, folder, models=models)

    assert report["rounds"] > 0, "the app is sent now, where it used to be skipped"
    receipt = read(folder / "world/REPAIR.json")
    assert "is repaired from a sample" in receipt["receipts"]["sampled"]["gmail_mock"]
    sent = [p for p in models.payloads if p["call"] == "app_state"]
    assert sent, "an author call was actually made"
    for payload in sent:
        assert payload["previous_state_is_a_sample"] is True
        assert "ONLY the records you change" in payload["output_contract"]
        assert len(payload["previous_state_json"]) < len(json.dumps({"emails": before}))
    # Every prompt fits, where the unsampled one was 3,859,614 characters and refused 162 times.
    assert all(len(p) <= PROMPT_CEILING for p in (json.dumps(x) for x in sent))
    after = read(folder / "world/gmail_mock.state.json")["emails"]
    assert len(after) == len(before), "the records the sample left out are kept, not deleted"
    assert [e["id"] for e in after] == [e["id"] for e in before]
    # The defect is still reported -- this stub repairs nothing -- but the reason now says the
    # rounds were spent, not that no round could ever have been.
    assert report["ok"] is False
    assert any("repaired from a sample" in u and "round(s)" in u for u in report["unrepaired"]), report[
        "unrepaired"
    ]


def test_the_repair_reads_the_round_the_world_ships_not_the_last_one(rejected):
    """``review_world`` keeps the best of the rounds the decisive panel read and marks it ``kept``,
    because the last round's state was the one kept and 37 of the 38 worlds held at revise end above
    their best round (159 errors above it, against 253 in their last rounds). A repair that read the
    last round's findings against the kept round's state would hand the author a worklist of records
    it is not looking at."""
    from company_envs.world.world_repair import review_errors
    from company_envs.world.world_review import record_holders

    def round_of(index, findings, **extra):
        return {
            "round": index,
            "verdict": {"verdict": "revise", "summary": "reading", "findings": findings},
            "receipt": {"votes": 3, "cast": 3},
            **extra,
        }

    _root, folder = rejected([DRIFT_FINDING])
    holders = record_holders(read(folder / "world/world.json"), {"gmail_mock": {"emails": [EMAIL, DRIFTED]}})
    later = finding("gmail_mock", "A different slip the superseded round found.", "e1")
    review = {
        "rounds": [round_of(0, [DRIFT_FINDING], kept="ships this round"), round_of(1, [later])],
        "final_verdict": "revise",
    }

    assert [f.issue for f in review_errors(review, holders)] == [DRIFT_FINDING["issue"]]
    # With no round marked, the last round is the one on disk, as it always was.
    review["rounds"][0].pop("kept")
    assert [f.issue for f in review_errors(review, holders)] == [later["issue"]]
    assert review_errors({"rounds": []}, holders) == []


def test_the_verdict_whose_inputs_are_compared_is_the_one_the_world_ships(rejected):
    """The rounds after the one ``review_world`` marked ``kept`` describe a state that was discarded,
    so comparing their inputs against disk reports drift the pipeline created by keeping the better
    round. The kept round's reading is the one the world ships on."""
    _root, folder = rejected([DRIFT_FINDING])
    on_disk = read(folder / "world/gmail_mock.state.json")
    review = read(folder / "world/REVIEW.json")
    review["rounds"][0]["inputs"] = review_inputs(
        read(folder / "world/world.json"), {"gmail_mock": on_disk}, []
    )
    review["rounds"][0]["kept"] = "ships this round"
    review["rounds"].append(
        {
            "round": 1,
            "verdict": {
                "verdict": "revise",
                "summary": "a reading of a state since discarded",
                "findings": [],
            },
            "receipt": {"votes": 3, "cast": 3},
            "inputs": review_inputs(read(folder / "world/world.json"), {"gmail_mock": {"emails": []}}, []),
        }
    )
    write(folder / "world/REVIEW.json", review)

    assert stale_verdict(folder) == [], "the kept round read exactly what is on disk"
    assert verdict_coverage(folder) == 1.0

    review["rounds"][0].pop("kept")
    write(folder / "world/REVIEW.json", review)
    assert verdict_coverage(folder) == 0.0, "unmarked, the discarded round's empty reading is taken"


def test_a_half_laid_set_of_layers_is_never_written_under_bookkeeping_for_the_old_one(tmp_path):
    """``lay_layers`` laid, validated and wrote one app at a time with BULK.json last, so an app whose
    laid state failed its schema left the apps before it on disk under bookkeeping naming the layer
    those apps had replaced. That is the same defect found in the post-lay repair -- a fault between
    writing state and writing the receipt -- and it closes by doing everything that can fail before
    anything is written."""
    import json as _json

    from company_envs.world.world_repair import lay_layers

    root, folder = _seeded_company(tmp_path, [EMAIL])
    apps = read(folder / "apps.json")["apps"]
    good = read(folder / "world/gmail_mock.state.json")
    before = (folder / "world/gmail_mock.state.json").read_bytes()
    # A second app whose laid state will fail validation, ordered after the first.
    apps = [*apps, {**apps[0], "app_id": "other_mock", "top_level_keys": ["nope"]}]
    (folder / "world/other_mock.state.json").write_text(_json.dumps({"emails": []}))
    # gmail's human layer has a record the layer on disk does not, so it is a layer that must be
    # written; other_mock's laid state will fail its schema after it.
    human = {
        "gmail_mock": {**good, "emails": [*good["emails"], {**EMAIL, "id": "new"}]},
        "other_mock": {"emails": []},
    }
    layered = {"gmail_mock": good, "other_mock": {"emails": []}}
    bulk = {"apps": {"other_mock": {"specs": [spec(count=1, template=STUB_TEMPLATE)]}}}
    with pytest.raises(ValueError):
        lay_layers(root, folder, {"design": {}, "generation": {}}, apps, bulk, {}, layered, human)
    assert (folder / "world/gmail_mock.state.json").read_bytes() == before, (
        "the app before the failure is untouched, so no BULK.json can name a layer it replaced"
    )
    assert not (folder / "world/BULK.json").exists()


def test_a_fault_writes_no_receipt_so_the_step_runs_again(tmp_path, monkeypatch):
    """A ``ModelOutputInvalid`` or a ``ValueError`` is a verdict about this world: record it and write
    the receipt, because a repair that crashed silently was indistinguishable from a world that needed
    none. An ``OSError`` is a fault and says nothing about a company -- it was in the same catch, so a
    disk error wrote a fresh REPAIR.json over a world the fault had left half-written, and the marker
    then kept the step from ever running again."""
    from company_envs.world import world_repair

    root, folder = _seeded_company(tmp_path, [EMAIL, BAD_BODY])

    def explode(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(world_repair, "lay_layers", explode)
    with pytest.raises(OSError, match="no space left"):
        world_repair.repair_world(root, folder, models=Models())
    assert not (folder / "world/REPAIR.json").exists(), "no marker, so the step is still stale"
