from pathlib import Path

import pytest

from company_envs.world.python_verifier import VerifierUnavailable, run_verifier
from company_envs.world.verifier_runner import validate


@pytest.mark.parametrize(
    "code",
    [
        "import os\ndef verify(a,b,c,d): return {}",
        "def verify(a,b,c,d): return open('/etc/passwd').read()",
        "def verify(a,b,c,d): return a.__class__",
        "def verify(a,b,c,d): return eval('1')",
        "from math import *\ndef verify(a,b,c,d): return {}",
    ],
)
def test_verifier_rejects_files_reflection_and_unapproved_imports(code):
    with pytest.raises(ValueError):
        validate(code)


def test_isolated_verifier_preserves_inputs_and_reports_real_business_change(tmp_path):
    code = tmp_path / "verifier.py"
    code.write_text(
        "def verify(initial, final, files, events):\n    return {'1': {'passed': final['case']['status'] == 'resolved' and initial != final, 'reason': 'Observed case status'}}\n"
    )
    original = code.read_bytes()
    data = {"initial": {"case": {"status": "open"}}, "final": {"case": {"status": "open"}}}
    assert not run_verifier(code, data, [1])["checks"]["1"]["passed"]
    data["final"]["case"]["status"] = "resolved"
    assert run_verifier(code, data, [1])["checks"]["1"]["passed"]
    assert code.read_bytes() == original and not Path("/input.json").exists()


def test_verifier_crash_is_unavailable_instead_of_a_task_failure(tmp_path):
    code = tmp_path / "verifier.py"
    code.write_text("def verify(initial, final, files, events):\n    return 1 / 0\n")
    with pytest.raises(VerifierUnavailable, match="ZeroDivisionError"):
        run_verifier(code, {"initial": {}, "final": {}}, [1])


def test_verifier_missing_criterion_is_unavailable(tmp_path):
    code = tmp_path / "verifier.py"
    code.write_text("def verify(initial, final, files, events):\n    return {}\n")
    with pytest.raises(VerifierUnavailable, match="cover every"):
        run_verifier(code, {"initial": {}, "final": {}}, [1])
