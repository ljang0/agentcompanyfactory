"""The seed contract is the app's, not the document's.

Seeding reads the `## State Schema` table (`hub_app.top_level_keys`) and `validate_state` refuses
any key it does not list, so a key the app keeps but the table omits can never be seeded: the app
falls back to the demo records it ships with, and on load it writes those into the company's
world. On 2026-09-09 that was true of 37 of 88 apps, aws_console by 19 keys.

These tests hold the tables to what the apps were observed to keep. The observations are recorded
by `scripts/app_schema_drift.py`, which serves each app unseeded and asks it; they are evidence in
the tree, so this runs offline and needs no browser.
"""

import json
from pathlib import Path

import pytest

from company_envs.world.hub_app import hub_apps, permitted_keys

ROOT = Path(__file__).resolve().parent.parent
DRIFT_RUNS = sorted(path for path in (ROOT / "experiments/interact").glob("drift-*") if path.is_dir())
SCHEMAS = ROOT / "catalogs/app_schemas"


def observations():
    """Every recorded observation of what an app really keeps, by app id; the newest run wins.

    Every ``drift-*`` run is read, not one dated directory. Ten hub apps were ported on
    2026-09-10 -- five that `hub_apps()` refused for a missing schema document and five for an
    empty `state_keys` -- and their observations could not live in the 2026-09-09 directory
    without misdating them. Pinning one directory would have admitted all ten to the catalogue
    with nothing holding their tables to the apps, which is the gap that left 101 keys across 37
    apps unseedable.
    """
    found = {}
    for directory in DRIFT_RUNS:
        for path in sorted(directory.glob("*.json")):
            try:
                report = json.loads(path.read_text())
            except ValueError:
                continue
            # An app that would not serve, or kept nothing, proves nothing about its table.
            if report.get("observed") and not str(report.get("source", "")).startswith("error"):
                found[report["app_id"]] = report
    return found


RECORDED = observations()


def test_the_observations_cover_the_catalogue():
    catalogue = set(hub_apps(json.loads((ROOT / "catalogs/apps.json").read_text())["apps"]))
    missing = sorted(catalogue - set(RECORDED))
    # PACS-viewer keeps no state until a study is opened, so it has nothing to observe.
    assert len(missing) <= 2, f"apps with no recorded observation: {missing}"


@pytest.mark.parametrize("app_id", sorted(RECORDED))
def test_every_key_the_app_keeps_can_be_seeded(app_id):
    schema = SCHEMAS / f"{app_id}.md"
    if not schema.is_file():
        pytest.skip(f"{app_id} has no pinned schema document")
    allowed = set(permitted_keys(app_id, schema.read_text()) or [])
    unseedable = [key for key in RECORDED[app_id]["observed"] if key not in allowed]
    assert not unseedable, (
        f"{app_id} keeps {unseedable}, and neither its imported schema nor "
        f"catalogs/app_schema_supplement.json permits them, so seeding cannot fill them and the "
        f"app will show its own demo records there. Regenerate the supplement from "
        f"experiments/interact/{DRIFT_RUNS[-1].name}."
    )


@pytest.mark.parametrize("app_id", sorted(RECORDED))
def test_a_documented_key_the_app_never_keeps_is_recorded_as_such(app_id):
    """Not a failure -- a key can appear only once somebody acts -- but it must stay visible.

    These are seeded records nothing reads. zoom_web's `channels` and `messages` are the known
    case: the schema declares them, the app has neither, and `TeamChat.jsx` reads `channels`
    regardless.
    """
    report = RECORDED[app_id]
    assert report.get("documented_but_app_keeps_none") is not None
