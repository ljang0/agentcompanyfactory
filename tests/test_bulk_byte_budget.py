"""The seeded byte budget, per app, must stay inside the ceiling each app was measured against.

``design.bulk_max_bytes_per_app`` is handed to the model that writes bulk specs, which the skill
tells to spend "most of the byte budget the payload states". App volume therefore *equals* this
number, and raising it raises every heavy app's state to match: in the unit the budget is measured
in (``len(json.dumps(state))``, compact) no state in the fleet exceeded 4.09 MB against a 4,000,000
cap, and 22 of 406 sat within 2% of it. What is seeded cannot set the number, because it mirrors
the number; only the apps can, and two different ceilings bind them.

*Storage.* ``localStorage`` takes 5 MiB of ``JSON.stringify`` characters per origin. Measured over
136 probes in ``experiments/VOLUME-CEILINGS.md``, a 5.23 MB state still cached and 5.36 MB cached
nothing. 25 of the 26 apps probed do not care -- their fetch is gated on the state key itself, so a
missing key sends them back to the server, and they render, write and read back unchanged at 12 MB.
``google_docs_mock`` gates on a separate ``_initial_`` key that its own demo data writes at module
load, so past the quota it never fetches: at 5.03 MB it passes every check, and at 5.36 MB the
first reload replaced a company's 1,947 documents with the five the clone ships ("Project
Proposal", "Meeting Notes - Feb 14") and posted them back as the state, taking a typed write with
them. Every other budget truncates a world when it is wrong; that one corrupts it.

*The clock.* Most apps virtualise and cost nothing extra to 12 MB. ``gmail_mock`` renders the whole
mailbox -- 578,099 characters of visible text at 12 MB, about +3 s of page load per MB -- so its
budget is bought against the 25-minute per-worker episode, not against storage.

A blanket 6,000,000 was proposed twice on fleet-wide pass rates. These tests are the guard those
numbers did not provide.
"""

import tomllib
from pathlib import Path

import pytest

from company_envs.world.bulk_layer import (
    CEILING_REASONS,
    DEFAULT_MAX_BYTES,
    HARD_MAX_BYTES,
    max_bytes_for,
)

REPO = Path(__file__).resolve().parent.parent

# localStorage per origin, in JSON.stringify characters. Chrome's 5 MiB, confirmed by probe: the
# largest cache that landed was 5,229,000 characters and 5.36 MB landed nothing.
BROWSER_QUOTA = 5 * 1024 * 1024
# What a 25-minute episode can add to one app before a seeded state reaches the quota. Records run
# 533-2,047 bytes across the heavy apps, so this is several hundred of the largest of them.
EPISODE_HEADROOM = 500_000

# The largest state at which each app still rendered its company's records, took a typed write into
# the state a grader reads, and still held that write after a reload. From the sweep ladder in
# experiments/volume-ceilings-20260910/runs/ -- 2, 3, 4, 4.2, 4.5, 4.8, 5.1, 5.5, 6, 8 and 12 MB.
MEASURED_CEILINGS = {
    "slack_mock": 12_280_000,
    "google_drive_mock": 12_110_000,
    "shopify_admin_mock": 11_990_000,
    "gmail_mock": 12_070_000,
    "google_calendar_mock": 12_150_000,
    "trello_mock": 12_150_000,
    "robinhood_mock": 12_100_000,
    "clio_mock": 12_060_000,
    "google_docs_mock": 5_030_000,
}


@pytest.fixture
def design():
    return tomllib.loads((REPO / "config.toml").read_text())["design"]


def test_the_default_leaves_an_episode_room_under_the_browser_quota(design):
    budget = design["bulk_max_bytes_per_app"]
    assert budget <= BROWSER_QUOTA - EPISODE_HEADROOM, (
        f"design.bulk_max_bytes_per_app = {budget} seeds app states that cannot be cached once an "
        f"episode adds to them. google_docs_mock then loads its shipped demo documents instead of "
        f"the company's and posts them back as the state (measured at 5.36 MB; 5.03 MB passes). "
        f"Raise a single app through design.bulk_max_bytes_by_app, not by moving the default."
    )


def test_the_default_is_worth_spending_a_seed_call_on(design):
    """The other side of the same number: below this the heavy apps hold less than a week.

    gmail's emails measure 1,146 bytes each and slack's messages 590, so 3,000,000 buys a mailbox
    of 2,617 messages -- three weeks of one shared corporate inbox at ~120 messages a business day.
    A budget that low is not a company's volume, and lowering it is as much a change as raising it.
    """
    assert design["bulk_max_bytes_per_app"] >= 3_000_000


def test_every_per_app_budget_stays_under_what_that_app_was_measured_to_hold(design):
    for app_id, budget in design["bulk_max_bytes_by_app"].items():
        assert app_id in MEASURED_CEILINGS, (
            f"{app_id} has a budget but no measured ceiling. A number nobody probed is the guess "
            f"this table exists to replace; sweep it first (experiments/VOLUME-CEILINGS.md)."
        )
        assert budget <= MEASURED_CEILINGS[app_id], (
            f"{app_id} is budgeted {budget} against a measured ceiling of "
            f"{MEASURED_CEILINGS[app_id]}, past which it stops rendering, writing or reading back."
        )


def test_a_mapped_app_says_which_ceiling_it_is_up_against(design):
    """Storage and the clock have different remedies, so an entry that does not say which is a trap.

    gmail is the case in point: it is storage-clean to 12.07 MB, and its 6,000,000 is bought
    against the episode's 25 minutes at about +3 s of page load per MB. A reader who takes that for
    a quota limit raises it and pays in worker time instead of volume.
    """
    for app_id in design["bulk_max_bytes_by_app"]:
        assert app_id in CEILING_REASONS, f"{app_id} is budgeted without saying why"
    assert "clock" in CEILING_REASONS["gmail_mock"]


def test_google_docs_cannot_be_raised_by_editing_a_number():
    """The one app where a wrong budget corrupts a world instead of truncating it.

    Measured: 5.03 MB passes every check, 5.36 MB replaces the company's 1,947 documents with the
    clone's five demo documents on the first reload and posts them back over the world.
    """
    assert "google_docs_mock" not in tomllib.loads((REPO / "config.toml").read_text())["design"].get(
        "bulk_max_bytes_by_app", {}
    ), "google_docs_mock's ceiling lives in bulk_layer.HARD_MAX_BYTES, not in config"
    reckless = {
        "design": {
            "bulk_max_bytes_per_app": 12_000_000,
            "bulk_max_bytes_by_app": {"google_docs_mock": 99_000_000},
        }
    }
    assert max_bytes_for(reckless, "google_docs_mock") == HARD_MAX_BYTES["google_docs_mock"]
    assert HARD_MAX_BYTES["google_docs_mock"] <= MEASURED_CEILINGS["google_docs_mock"]


def test_the_clamp_only_ever_lowers():
    """A hard ceiling must not raise a budget somebody deliberately set lower."""
    careful = {"design": {"bulk_max_bytes_per_app": 1_000_000}}
    assert max_bytes_for(careful, "google_docs_mock") == 1_000_000


def test_an_unmeasured_app_takes_the_default_and_nothing_else(design):
    config = {"design": design}
    assert max_bytes_for(config, "sentry_mock") == design["bulk_max_bytes_per_app"]
    assert max_bytes_for({}, "sentry_mock") == DEFAULT_MAX_BYTES
    assert max_bytes_for({"design": {}}, "sentry_mock") == DEFAULT_MAX_BYTES


def test_the_map_names_apps_that_exist(design):
    """A typo here is silent: the app simply takes the default and nobody is told."""
    runtime = set(design["available_runtime_apps"])
    assert set(design["bulk_max_bytes_by_app"]) <= runtime
    assert set(HARD_MAX_BYTES) <= runtime
    assert set(CEILING_REASONS) <= runtime
