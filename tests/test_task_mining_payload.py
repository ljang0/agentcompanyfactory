"""The task-authoring payload must fit the provider's prompt limit without hiding records."""

import json

from company_envs.world.task_author import PAYLOAD_BUDGET, bounded_snapshot

PROVIDER_LIMIT = 1_048_576


def snapshot(rows, width=200):
    """A snapshot whose one collection holds ROWS records of about WIDTH characters each."""
    return {
        "world": {"name": "acme"},
        "app_index": {
            "gmail_mock": {"emails": [{"id": f"m{i}", "subject": "s" * width} for i in range(rows)]}
        },
        "standard_apps": ["gmail_mock"],
        "provenance": {},
    }


def test_an_index_that_already_fits_is_left_exactly_as_it_is():
    original = snapshot(3)
    assert bounded_snapshot(original)["app_index"] == original["app_index"]


def test_an_index_over_the_budget_is_cut_and_says_how_much_it_left_out():
    bounded = bounded_snapshot(snapshot(40_000))
    emails = bounded["app_index"]["gmail_mock"]["emails"]
    assert len(json.dumps(bounded)) <= PAYLOAD_BUDGET
    assert emails[-1]["id"] == "__more__" and emails[-1]["omitted_records"] > 0
    assert emails[0]["id"] == "m0"  # the head is kept, so the oldest records are the ones dropped


def test_it_trims_only_as_far_as_it_must():
    """Records are what the designer reasons about, so a bigger index is kept when it fits."""
    roomy = bounded_snapshot(snapshot(4_000))["app_index"]["gmail_mock"]["emails"]
    crowded = bounded_snapshot(snapshot(40_000, width=2_000))["app_index"]["gmail_mock"]["emails"]
    assert len(roomy) > len(crowded)


def test_bounding_does_not_touch_the_snapshot_validation_reads():
    original = snapshot(40_000)
    bounded_snapshot(original)
    assert len(original["app_index"]["gmail_mock"]["emails"]) == 40_000


def test_everything_outside_the_index_survives():
    original = snapshot(1)
    bounded = bounded_snapshot(original)
    assert bounded["world"] == original["world"] and bounded["standard_apps"] == ["gmail_mock"]


def test_the_biggest_real_world_index_would_fit_the_prompt_limit():
    """The failure that stopped every mining call: one company indexed over the provider's limit."""
    huge = snapshot(60_000)
    assert len(json.dumps(huge)) > PROVIDER_LIMIT
    assert len(json.dumps(bounded_snapshot(huge))) < PROVIDER_LIMIT


def test_a_collection_too_wide_for_the_floor_still_comes_back_under_the_budget():
    """The halving stopped at 25 and returned that cap unchecked: 25 records of 60,000 characters
    each serialize to 1,500,000, so the budget held only until a collection was wide enough."""
    wide = snapshot(25, width=60_000)
    assert len(json.dumps(wide["app_index"])) > PROVIDER_LIMIT
    assert len(json.dumps(bounded_snapshot(wide))) <= PAYLOAD_BUDGET


def test_the_hashes_and_the_full_states_stay_out_of_the_model_payload():
    """Provenance is 6,357-9,268 characters of sha256 per company and the states are hundreds of
    thousands more; both are read locally, and read_seed serves the states instead of the prompt."""
    original = snapshot(1) | {
        "provenance": {"world/world.json": "a" * 64},
        "states": {"gmail_mock": {"emails": [{"id": "m0", "body": "b" * 5_000}]}},
    }
    bounded = bounded_snapshot(original)
    assert "provenance" not in bounded and "states" not in bounded
    assert original["provenance"] and original["states"]  # the caller still has both


def test_the_budget_bounds_the_whole_payload_and_not_the_index_inside_it():
    """The budget used to apply to ``app_index`` alone, so the canonical world went unbounded.

    Measured at HEAD over the 12 companies whose mining was refused: their canonical worlds are
    109,316-133,695 characters and their record counts another 1,427-2,671, neither of them inside
    the old bound, and 2 of the 12 still come back over the old 600,000. The refused prompts carried
    878,083-1,254,675 characters of payload against a 1,048,576 ceiling.
    """
    world_only = {
        "world": {"emails": [{"id": f"w{i}", "body": "y" * 400} for i in range(4_000)]},
        "app_index": {"gmail_mock": {"emails": [{"id": "m0"}]}},
        "record_counts": {"gmail_mock.emails": 1},
        "provenance": {},
    }
    assert len(json.dumps(world_only)) > PAYLOAD_BUDGET
    bounded = bounded_snapshot(world_only)
    assert len(json.dumps(bounded)) <= PAYLOAD_BUDGET
    # The index was already one record; it is the world that was sampled, and it says so.
    assert bounded["app_index"] == world_only["app_index"]
    kept = bounded["world"]["emails"]
    assert len(kept) < 4_000 and any("_sampled" in row for row in kept if isinstance(row, dict))
    assert len(world_only["world"]["emails"]) == 4_000, "the caller still has the whole world"


def test_a_mining_run_that_published_nothing_says_which_of_the_four_it_was():
    """`MINE.json` recorded `requested: 2, accepted: []` with no reason, identical to a designer that
    simply accepted nothing. There are three different empty runs here and they need three answers.

    The over-ceiling refusal is a verdict no retry can change, which is why `oversized_mining` returns
    instead of raising -- raising had the step rebuild the same payload every loop, 12 call ids across
    12 companies reaching 234 attempts. A review that turned every design down is also a verdict, but
    about the designs rather than the payload. A provider outage is neither, and `from_exception` is
    what keeps it from being filed as either.
    """
    from company_envs.models import ModelUnavailable, PromptTooLarge
    from company_envs.receipt import Receipt, outcome_of
    from company_envs.world.task_author import _nothing_mined

    report = _nothing_mined("skill@1", "review@1", "design: prompt is 3,859,614 characters ...")
    assert outcome_of(report) == "refused" and report["ok"] is False
    assert report["accepted"] == [] and report["oversized"] == report["reason"]
    # The shape it was indistinguishable from, and the one it must stay distinguishable from.
    assert outcome_of({"authored_at": "now", "accepted": [], "rejected": []}) == "unmeasured"
    # And the split the receipt type takes from models.py, on this stage's own two exceptions.
    assert Receipt.from_exception(PromptTooLarge("design", 3_859_614), "mine").outcome == "refused"
    assert Receipt.from_exception(ModelUnavailable("503"), "mine").outcome == "faulted"
