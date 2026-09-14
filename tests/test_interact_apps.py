"""What the app-interaction runner files, and what it must refuse to file.

The runner's own defect class is the one the correlated findings put first: an empty run writing
the receipt a real verdict writes. ``canvas_mock`` filed a ``mode: volume`` report over a state
that ``--target-mb 3`` had left at 1,764 bytes, because its only collection is nested inside
another object and amplification never reached it.
"""

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "interact_apps.py"
spec = importlib.util.spec_from_file_location("interact_apps", SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def a_finished_report():
    return {
        "app_id": "canvas_mock",
        "checks": {"first_screen_not_empty": True},
        "failed_checks": [],
        "passed": True,
        "mode": "volume",
    }


def test_a_run_that_never_reached_the_target_size_files_no_volume_verdict():
    """canvas_mock's 1,764 bytes against a 3 MB target passed as a volume test of the app. The
    run's other findings are worth keeping; the claim that volume was tested is not."""
    report = a_finished_report()
    runner.record_volume_reach(
        report,
        {
            "start_bytes": 1764,
            "bytes": 1764,
            "target_bytes": 3_145_728,
            "grew": False,
            "reached_target": False,
        },
    )
    assert report["mode"] != "volume", "nothing reading for a volume verdict may find one here"
    assert report["volume_tested"] is False
    assert report["passed"] is False
    assert "state_reached_volume_target" in report["failed_checks"]
    assert "1764" in report["note"] and "3145728" in report["note"]
    assert report["checks"]["first_screen_not_empty"] is True, "what the run did see is kept"


def test_a_run_that_reached_the_target_size_files_the_volume_verdict_it_always_did():
    report = a_finished_report()
    runner.record_volume_reach(
        report,
        {
            "start_bytes": 1764,
            "bytes": 3_549_320,
            "target_bytes": 3_145_728,
            "grew": True,
            "reached_target": True,
        },
    )
    assert report["mode"] == "volume"
    assert report["volume_tested"] is True
    assert report["passed"] is True
    assert report["failed_checks"] == []
    assert report["amplification"]["bytes"] == 3_549_320


def test_the_state_origin_says_which_of_the_two_a_report_is(tmp_path):
    """A report is read by whoever finds it, and ``fixture+amplified`` was printed over a state
    that had not been amplified at all."""
    fixtures = tmp_path / "states"
    fixtures.mkdir()
    (fixtures / "canvas_mock.json").write_text(json.dumps({"canvasJSON": {"objects": [{"id": "a"}]}}))
    (fixtures / "settings_mock.json").write_text(json.dumps({"ui": {"view": "week"}}))

    _, origin, reached = runner.pick_state("canvas_mock", "volume", fixtures, tmp_path, 60_000)
    assert origin == "fixture+amplified" and reached["reached_target"]

    _, origin, reached = runner.pick_state("settings_mock", "volume", fixtures, tmp_path, 60_000)
    assert origin == "fixture+not-amplified" and not reached["reached_target"]


def test_fidelity_mode_makes_no_amplification_claim_either_way(tmp_path):
    state, origin, reached = runner.pick_state("absent_mock", "fidelity", tmp_path, tmp_path, 60_000)
    assert (state, origin, reached) == (None, "none", None)
