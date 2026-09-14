"""The evidence table's cells, and the one rule the table exists to keep.

A dimension with no artifact reads `-`, which means no evidence either way -- never "works". Nine of
the fourteen columns were hand-rolled `None if not report else ...` chains, and the rule broke in the
one where the chain asked whether a report existed instead of whether it had measured anything:
`canvas_mock` filed a `mode: volume` report having never grown past 1,764 bytes, because its only
collection is nested and the amplifier could not reach it, and the table printed that as a volume
**pass**. `cell` is the question asked once, through `company_envs.receipt.gate`.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "app_evidence.py"
spec = importlib.util.spec_from_file_location("app_evidence", SCRIPT)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.mark.parametrize(
    ("taken", "ok", "expected", "shown"),
    [
        (True, True, True, "yes"),
        (True, False, False, "NO"),
        # The whole point: a run that never reached its target is no evidence, whatever it claims.
        (False, True, None, "-"),
        (False, False, None, "-"),
    ],
)
def test_a_cell_is_no_evidence_unless_the_measurement_was_taken(taken, ok, expected, shown):
    assert evidence.cell(taken, ok) is expected
    assert evidence.mark(evidence.cell(taken, ok)) == shown


def test_the_volume_cell_refuses_a_report_that_never_reached_its_target():
    """The measured instance, as the report on disk carries it: `volume_tested: false` with every
    check passing, because nothing was ever grown. A report with no checks failing is a pass only
    once the growth happened."""
    untested = {"volume_tested": False, "failed_checks": []}
    assert evidence.cell(untested.get("volume_tested") is not False, True) is None
    grown = {"volume_tested": True, "failed_checks": []}
    assert evidence.cell(grown.get("volume_tested") is not False, True) is True
    broke = {"volume_tested": True, "failed_checks": ["first_screen_no_error"]}
    assert (
        evidence.cell(
            broke.get("volume_tested") is not False,
            not (set(broke["failed_checks"]) & set(evidence.BROKEN)),
        )
        is False
    )


def test_the_marks_are_the_shared_outcome_names_so_a_fault_has_somewhere_to_go():
    """None of these reports records an environment fault today, and folding one into "NO" would be
    this defect again -- a fault says nothing about the app. The mark exists before the report does."""
    from company_envs.receipt import OUTCOMES

    assert set(evidence.MARKS) == set(OUTCOMES)
    assert evidence.MARKS["faulted"] != evidence.MARKS["refused"]
    assert evidence.MARKS["unmeasured"] == "-", "a gap is as visible as a pass"
