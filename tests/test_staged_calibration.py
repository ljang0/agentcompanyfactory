from company_envs.world.staged_calibration import classify_result


def test_unrelated_failure_cannot_hide_semantic_false_positive():
    report = {
        "passed": False,
        "complete": True,
        "semantic_checks": [{"criterion": 1, "status": "fail"}, {"criterion": 2, "status": "pass"}],
        "dependencies": [],
    }
    assert (
        classify_result(report, "wrong_total", {"criteria": [2], "workers": []}) == "semantic_false_positive"
    )


def test_inconclusive_target_is_not_a_successful_negative_control():
    report = {
        "passed": False,
        "complete": False,
        "semantic_checks": [{"criterion": 2, "status": "pending"}],
        "dependencies": [],
    }
    assert classify_result(report, "wrong_total", {"criteria": [2], "workers": []}) == "unavailable"


def test_missing_target_evidence_is_unavailable():
    report = {"passed": False, "complete": True, "semantic_checks": [], "dependencies": []}
    assert classify_result(report, "wrong_total", {"criteria": [2], "workers": []}) == "unavailable"


def test_legitimately_satisfied_criteria_do_not_invalidate_targeted_negative():
    report = {
        "passed": False,
        "complete": True,
        "semantic_checks": [{"criterion": 1, "status": "pass"}, {"criterion": 2, "status": "fail"}],
        "dependencies": [],
    }
    assert classify_result(report, "wrong_total", {"criteria": [2], "workers": []}) == "fail"
