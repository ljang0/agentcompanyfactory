"""The shared receipt type, and the defect class it exists to make unwritable.

Eight instances before the 2026-09-10 sweep and seven more during it, in seven files, all one
sentence: *an empty, skipped or faulted run writes the receipt a real verdict writes.* Every fix was
local. These tests are about the thing a local fix cannot do -- making the next stage unable to get
it wrong -- so each one names the measured instance it generalises.
"""

import pytest

from company_envs.models import (
    CallBudgetExhausted,
    ModelOutputInvalid,
    ModelUnavailable,
    PromptTooLarge,
)
from company_envs.receipt import (
    FAULTED,
    MEASURED,
    OUTCOME_OK,
    OUTCOMES,
    PASSED,
    REFUSED,
    UNMEASURED,
    Receipt,
    attempt,
    faulted,
    gate,
    measured,
    missing_of,
    ok_of,
    outcome_of,
    passed,
    reason_of,
    rollup,
    summary,
)


def test_the_outcome_cannot_be_omitted():
    """The field has no default, which is the whole design.

    Every one of the fifteen instances was a receipt whose author never had to say which of the four
    it was, so the reader assumed the generous one: `canvas_mock` filed a volume verdict at 1,764
    bytes, `MINE.json` recorded `requested: 2, accepted: []` with no reason, and a teacher that never
    passed left `launch` reading `status: done`.
    """
    with pytest.raises(TypeError):
        Receipt()
    with pytest.raises(ValueError, match="outcome must be one of"):
        Receipt("skipped")
    assert set(OUTCOMES) == {PASSED, REFUSED, FAULTED, UNMEASURED}


def test_ok_keeps_the_meaning_every_gate_on_disk_already_reads():
    """`ok` is derived, so no gate had to change: True, False, False, None.

    `unmeasured` is `ok: None` rather than False because `all()` over findings must still refuse a
    report containing one -- the shape barrier.py arrived at, and the reason the 30 of 39 unseeded
    companies that reported `barrier ok: true` now do not.
    """
    assert OUTCOME_OK == {PASSED: True, REFUSED: False, FAULTED: False, UNMEASURED: None}
    assert Receipt.passed().ok is True
    assert Receipt.refused("no").ok is False
    assert Receipt.faulted("outage").ok is False
    assert Receipt.unmeasured(["world/SEED.json"], "the mailbox barrier").ok is None
    assert MEASURED == (PASSED, REFUSED), "a fault and an unmeasured run are not evidence"


def test_a_verdict_without_a_reason_and_an_unmeasured_run_without_its_inputs_are_refused():
    """`SEED_FAILED.json` recorded `{at, count, rc}` and no reason, 37 of 37 on disk, 13 of them from
    a signal death that says nothing about the work. A receipt that cannot be acted on is the same as
    no receipt, so the constructor will not build one."""
    for build in (Receipt.refused, Receipt.faulted):
        with pytest.raises(ValueError, match="must say why"):
            build("")
    with pytest.raises(ValueError, match="must name the inputs"):
        Receipt(UNMEASURED, reason="nothing was there")
    with pytest.raises(ValueError, match="was waiting for"):
        Receipt.unmeasured([], "the desktop barrier")


def test_a_pass_cannot_be_missing_its_inputs_or_smuggle_ok_past_a_refusal():
    """The readability gate wrote a **pass** over a world that had been moved aside -- plain/plain/
    plain over nothing. A run that could not read its inputs has measured nothing, not passed, and
    the two fields that say so cannot be set against each other."""
    with pytest.raises(ValueError, match="has not passed"):
        Receipt(PASSED, missing=("world/identities.json",))
    with pytest.raises(ValueError, match="may not set"):
        Receipt.refused("specs invalid", ok=True)
    with pytest.raises(ValueError, match="may not set"):
        Receipt.passed(outcome="passed")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PromptTooLarge("design", 3_859_614), REFUSED),
        (ModelOutputInvalid("two offenders", raw="{}"), REFUSED),
        (ValueError("the task needs a manager"), REFUSED),
        (ModelUnavailable("at capacity", retryable=True), FAULTED),
        (CallBudgetExhausted(653, 600), FAULTED),
        (OSError("no such file"), FAULTED),
        (RuntimeError("another coordinator owns this run"), FAULTED),
        (KeyError("messageId"), FAULTED),
    ],
)
def test_the_split_is_the_type_hierarchy_models_py_already_encodes(error, expected):
    """Three agents independently reinvented the verdict/fault split before one noticed it was in the
    type system: `PromptTooLarge` and `ModelOutputInvalid` are `ValueError`, `ModelUnavailable` and
    `CallBudgetExhausted` are `RuntimeError`.

    This test is the seam between the two modules. If one of those four is ever re-parented, a
    provider outage starts writing the receipt a refusal writes -- which is how `bulk_layer`'s strip
    destroyed a layer that was fine -- and this fails rather than the fleet finding out.
    """
    answer = Receipt.from_exception(error, "bulk specs")
    assert answer.outcome == expected
    assert type(error).__name__ in answer.reason
    assert answer.detail["error_type"] == type(error).__name__


def test_attempt_gives_a_failure_its_receipt_instead_of_a_line_on_stderr():
    """`extend_collection` skipped a shard with only a stderr line, so a collection that lost its
    history read exactly like one that never had any. A stage with no failure receipt is an infinite
    retry."""
    value, answer = attempt("shard 2", lambda: {"emails": []})
    assert value == {"emails": []} and answer is None

    def refuse():
        raise ValueError("shard window holds no new records")

    value, answer = attempt("shard 2", refuse, collection="emails")
    assert value is None
    assert answer.outcome == REFUSED and answer.detail["collection"] == "emails"

    def outage():
        raise ModelUnavailable("stream disconnected", retryable=True)

    assert attempt("shard 2", outage)[1].outcome == FAULTED


def test_gate_makes_a_caller_answer_whether_the_measurement_happened():
    """`canvas_mock` filed a `mode: volume` report having never grown past 1,764 bytes, and
    app_evidence printed a volume run that never reached its target as a volume **pass**. Neither
    question has a default here, so a pass cannot be reached without saying the run happened."""
    with pytest.raises(TypeError):
        gate(ok=True, what="volume")  # taken is not optional
    assert gate(taken=True, ok=True, what="volume").outcome == PASSED
    assert gate(taken=True, ok=False, what="volume").outcome == REFUSED
    unmeasured = gate(taken=False, ok=True, what="volume", missing=["4.4 MB of state"])
    assert unmeasured.outcome == UNMEASURED, "ok is ignored when nothing was measured"
    assert unmeasured.missing == ("4.4 MB of state",)
    # Even with nothing named, it cannot become a pass: the constructor supplies a placeholder.
    assert gate(taken=False, ok=True, what="volume").outcome == UNMEASURED


def test_rollup_never_calls_nothing_a_pass_and_lets_a_fault_win():
    """`bool(tasks) and all(...)` is the hand-written version of this, and the hand-written version is
    what reported `barrier ok: true` for 30 of the 39 companies whose worlds did not exist: the
    desktop comparison found no collisions in a world with no desktops."""
    assert rollup([], "barrier findings").outcome == UNMEASURED
    assert rollup([Receipt.passed(), Receipt.passed()], "apps").outcome == PASSED
    # One unmeasured finding is enough: the run did not finish measuring.
    mixed = rollup([Receipt.passed(), Receipt.unmeasured(["world/materials"], "the desktop barrier")], "x")
    assert mixed.outcome == UNMEASURED and mixed.missing == ("world/materials",)
    # A refusal is a real negative verdict and must not be softened into "no evidence"...
    both = rollup([Receipt.refused("same bytes"), Receipt.unmeasured(["world/SEED.json"], "y")], "x")
    assert both.outcome == REFUSED and both.missing == ("world/SEED.json",)
    # ...and a fault outranks everything, because it invalidates the whole reading.
    worst = rollup([Receipt.passed(), Receipt.refused("r"), Receipt.faulted("socket died")], "apps")
    assert worst.outcome == FAULTED
    assert worst.detail["outcomes"] == {PASSED: 1, REFUSED: 1, FAULTED: 1}
    assert "socket died" in worst.reason


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        # The new field wins wherever it is present.
        ({"outcome": "faulted", "ok": True}, FAULTED),
        # barrier.py's shape.
        ({"ok": None, "unmeasured": True, "missing": ["world/SEED.json"]}, UNMEASURED),
        # bulk_layer.py's marker: a recorded fault outranks everything else it says.
        ({"added_at": "now", "apps": {}, "faults": {"trello_mock": "ModelUnavailable: 503"}}, FAULTED),
        ({"added": {}, "fault": "OSError: closed"}, FAULTED),
        # task_author.py's refused mining.
        ({"accepted": [], "oversized": "over the 1,048,576 ceiling"}, REFUSED),
        # the interact reports' volume claim.
        ({"mode": "volume", "volume_tested": False, "checks": {}}, UNMEASURED),
        # the seeder's status words.
        ({"status": "seeded_reviewed"}, PASSED),
        ({"status": "seeded_review_failed"}, REFUSED),
        # the controller's rows: done-with-skipped is a done row only to the resume loop.
        ({"status": "done", "skipped": True, "result": {}}, REFUSED),
        ({"status": "done"}, PASSED),
        ({"status": "failed", "error": "TimeoutError: step seconds budget exhausted"}, FAULTED),
        # 368 receipts left mid-flight: the run did not finish, so it measured nothing.
        ({"status": "running"}, FAULTED),
        # a skip that claims ok has declared itself harmless (calibration with no live apps).
        ({"ok": True, "skipped": True, "reason": "live apps only"}, PASSED),
        ({"skipped": "the specs never validated"}, REFUSED),
        # a grade that did not pass is a verdict about the episode.
        ({"passed": False, "score": 0.4}, REFUSED),
        ({"accepted": True}, PASSED),
        # the bare ok every gate on disk reads.
        ({"ok": True}, PASSED),
        ({"ok": False}, REFUSED),
        # The driver's STATUS.json view already uses the word `outcome` for a stage's return value
        # ("render_failed", 0, a repr of an exception). A word it does not recognise is not a pass:
        # the reader falls through to the legacy fields and, finding none, says it measured nothing.
        ({"stage": "render", "outcome": "render_failed"}, UNMEASURED),
        ({"stage": "mine", "outcome": 0}, UNMEASURED),
        # and no receipt at all is no evidence either way, not a failure.
        ({}, UNMEASURED),
        (None, UNMEASURED),
        ("not a receipt", UNMEASURED),
    ],
)
def test_a_reader_tells_the_four_apart_without_knowing_the_stage(receipt, expected):
    """The driver reads receipts from a dozen stages and each had its own vocabulary -- `skipped`,
    `refused`, `oversized`, `unmeasured`, `faults`, `note`, `status: done` with `skipped: true`.

    That is why the same defect could be fixed fourteen times without the fifteenth site being
    found: "did this run measure anything" was a different question per file.
    """
    assert outcome_of(receipt) == expected
    assert ok_of(receipt) is OUTCOME_OK[expected]
    assert passed(receipt) is (expected == PASSED)
    assert faulted(receipt) is (expected == FAULTED)
    assert measured(receipt) is (expected in MEASURED)


def test_the_reader_answers_for_the_receipt_type_itself_and_quotes_whichever_field_said_why():
    answer = Receipt.refused("the specs never validated", app="trello_mock")
    assert outcome_of(answer) == REFUSED and reason_of(answer) == "the specs never validated"
    assert summary(answer) == "refused: the specs never validated"
    assert summary({"ok": True}) == PASSED, "a pass needs no excuse"
    assert reason_of({"faults": {"a": "503", "b": "504"}}) == "a: 503; b: 504"
    assert reason_of({"note": "author-tasks declined the spare outlines"}).startswith("author-tasks")
    assert missing_of({"unmeasured": ["world/materials"]}) == ("world/materials",)
    assert missing_of({"ok": True}) == ()


def test_a_receipt_has_no_truth_value_because_three_of_four_outcomes_would_pass_it():
    """`if receipt:` is the defect in miniature: a refusal, a fault and an unmeasured run are all
    truthy objects. Asking costs a TypeError instead of a wrong answer."""
    with pytest.raises(TypeError, match="no truth value"):
        bool(Receipt.refused("no"))
    with pytest.raises(TypeError, match="no truth value"):
        if Receipt.faulted("outage"):  # pragma: no cover -- the raise is the point
            pass


def test_a_reason_is_for_a_human_and_is_bounded():
    """One provider error arrived as 3,859,614 characters and the receipt holding it is read by every
    later stage."""
    answer = Receipt.refused("x" * 5000)
    assert len(answer.reason) == 400
    assert len(answer.body["reason"]) == 400


def test_with_detail_adds_evidence_and_never_revises_the_outcome():
    answer = Receipt.refused("the specs never validated").with_detail(attempts=4)
    assert answer.outcome == REFUSED and answer.detail == {"attempts": 4}
    assert answer.body["outcome"] == REFUSED and answer.body["attempts"] == 4


def test_the_four_independent_rollups_agree_with_this_one_so_a_migration_is_safe():
    """The argument for this module's existence, made checkable.

    Four authors wrote `rollup` before it existed: `barrier` and `agreement` as `bool(tasks) and
    all(...)`, `bulk_layer` as a per-app verdict/fault split, and `readability` as its own `_RANK`
    with a worst-item roll-up. None was wrong; a shape four people reach for and none can reuse is a
    missing module.

    What a migrator needs to know is that `readability` and this module agree on the answer and
    differ only in where the empty case is handled: `readability._RANK` puts `unmeasured` at -1 as a
    sentinel for *item* aggregation while `_category_verdict` returns early for an empty category,
    and its `blocking` list then refuses `unmeasured` just as `ok: None` does here. So the semantics
    line up and a migration changes no verdict -- which is the only reason it is worth doing.
    """
    from company_envs.world.readability import _RANK as READABILITY_RANK
    from company_envs.world.readability import _category_verdict

    assert "unmeasured" in READABILITY_RANK, "a fourth author reached the same word independently"
    assert _category_verdict([]) == "unmeasured", "an empty category, the same way rollup([]) is"
    assert rollup([], "a category").outcome == UNMEASURED
    # And both refuse it: readability's BLOCKING_CATEGORIES test is `verdict not in (None, "plain")`,
    # which is this module's `ok is not True`.
    assert ok_of(rollup([], "a category")) is not True
    # The worst item wins in both, and a mixed category is its worst item's verdict.
    assert _category_verdict([{"verdict": "plain"}, {"verdict": "dense"}]) == "dense"
    assert rollup([Receipt.passed(), Receipt.refused("dense")], "items").outcome == REFUSED


def test_an_outcome_is_not_automatically_a_gate_input():
    """The correction that cost the most to find, pinned so it is not re-learned.

    The batch driver's accept slot is a *re-run trigger*, not a gate: a marker it refuses makes the
    step run again. `passed` on `world/SEED.json` was scoped as a one-line win and would have re-run
    the seed step on 50 of the 60 worlds on disk -- about 1,650 core-hours over worlds that already
    exist -- because their `status: seeded_review_failed` maps, correctly, to `refused`. The verdict
    was right; the remedy was not. `review_accepted` stops those companies for nothing.

    So this test asserts the mapping *and* the reason it must not be wired into that slot. What
    belongs there is the question `current` answers -- has the deciding code changed -- because
    re-running is exactly the remedy for that.
    """
    # The verdict is correct and must stay correct: a world the reviewers rejected has not passed.
    failed = {"seeded_at": "t", "status": "seeded_review_failed", "review_verdict": "revise"}
    assert outcome_of(failed) == REFUSED and passed(failed) is False
    assert passed({"seeded_at": "t", "status": "seeded_reviewed"}) is True
    # And the seed is the wrong kind of step to re-run on a refusal: the remedy for a rejected world
    # is a repair round, not another seed, which is a different stage answering a different question.
    from company_envs.world.render_check import current as render_current

    assert render_current({}) is False, "a changed decider is what an accept slot should refuse"
    assert render_current({"ok": True, "outcome": PASSED}) is False, "a verdict alone is not freshness"
