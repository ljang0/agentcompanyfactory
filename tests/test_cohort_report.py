"""Cohort totals describe recorded evidence, including unfinished companies."""

import json

import pytest

from company_envs.storage import read, write
from company_envs.world.agreement import CONDITIONS
from company_envs.world.cohort_report import cohort_report


def review_round(index, verdict, ballots):
    return {
        "round": index,
        "verdict": {"verdict": verdict, "findings": []},
        "receipt": {
            "votes": len(ballots),
            "accepts": ballots.count("accept"),
            "ballots": [{"verdict": v, "receipt": {"model": "fake"}} for v in ballots],
        },
    }


@pytest.fixture
def cohort(tmp_path):
    companies = tmp_path / "companies"
    for name, done in (("zulu", False), ("alpha", True)):
        folder = companies / name
        conditions = {key: {"ok": True} for key in reversed(CONDITIONS)}
        if not done:
            conditions["checks"] = {"ok": False}
            conditions["readable"] = {"ok": False}
        write(folder / "AGREEMENT.json", {"conditions": conditions, "done": done})
        write(folder / "world/SEED.json", {"status": "seeded_reviewed" if done else "seeded_review_failed"})
        write(
            folder / "world/CHECKS.json",
            {
                "ok": done,
                "errors": 0 if done else 5,
                "warnings": 1,
                "findings": [{"severity": "warning", "message": "Only a warning: keep it separate"}]
                + (
                    []
                    if done
                    else [
                        {
                            "severity": "error",
                            "source": "mail",
                            "path": f"emails/{n}",
                            "message": f"Missing recipient: email {n} | customer",
                        }
                        for n in reversed(range(4))
                    ]
                    + [
                        {
                            "severity": "error",
                            "source": "world",
                            "path": "people",
                            "message": "Shared identity; two workers",
                        }
                    ]
                ),
            },
        )
        write(
            folder / "world/REVIEW.json",
            {
                "final_verdict": "accept" if done else "revise",
                "rounds": [review_round(0, "revise", ["accept", "revise", "revise"])]
                + ([review_round(1, "accept", ["revise", "accept", "accept"])] if done else []),
            },
        )
        write(
            folder / "world/mail.state.json",
            {
                "emails": [{"id": n} for n in range(6)],
                "users": {"u1": {"id": "u1"}},
                "threads": {"t1": [{"id": "m1"}, {"id": "m2"}]},
                "currentUser": {"id": "u1", "name": "Jo"},
                "selected": ["e1", "e2"],
                "zoom": 100,
            },
        )
        write(folder / "world/BULK.json", {"apps": {"mail": {"added": {"emails": 4}}}})
        write(
            folder / "REPORT-readability.json",
            {
                "categories": {
                    "brief": {"verdict": "plain" if done else "system-log"},
                    "messages": "dense",
                }
            },
        )
        write(folder / "tasks/job/workflow.json", {"id": "job"})
        write(folder / "runtime/grades/job/calibration.json", {"accepted": done})
    write(companies / "alpha/tasks/_plain_brief/REWRITE.json", {"attempts": 1})
    write(companies / "alpha/tasks/_worker_apps/ASSIGN.json", {"attempts": 2})
    write(companies / "zulu/tasks/later/workflow.json", {"id": "later"})
    (companies / "zulu/tasks/_plain_brief").mkdir()
    (companies / ".hidden").mkdir()
    return companies


def test_two_company_report_counts_every_requested_kind_of_evidence(cohort):
    out = cohort / "report"
    result = cohort_report(cohort, out)
    assert result["company_count"] == 2 and result["done"] == 1
    assert list(result["conditions"]) == list(CONDITIONS)
    assert result["conditions"]["checks"] == {"passed": 1, "failed": 1, "missing": 0}
    assert result["conditions"]["seeded"] == {"passed": 2, "failed": 0, "missing": 0}
    assert result["review_by_round"] == [
        {"round": 0, "accepted": 0, "total": 2, "pass_rate": 0},
        {"round": 1, "accepted": 1, "total": 1, "pass_rate": 1},
    ]
    assert result["review_by_vote_pattern"] == [
        {"pattern": "1 accept, 2 revise", "accepted": 0, "total": 2, "pass_rate": 0},
        {"pattern": "2 accept, 1 revise", "accepted": 1, "total": 1, "pass_rate": 1},
    ]
    errors = result["mechanical_error_classes"]
    assert [(e["message"], e["count"]) for e in errors] == [("Missing recipient", 4), ("Shared identity", 1)]
    assert [e["path"] for e in errors[0]["examples"]] == ["emails/0", "emails/1", "emails/2"]
    assert len(errors[1]["examples"]) == 1
    assert result["records_per_app"] == {"mail": {"human": 10, "bulk": 8, "total": 18}}
    assert result["calibration"] == {"accepted": 1, "failed": 1, "missing": 1, "total": 3, "pass_rate": 1 / 3}
    assert [(c["company"], c["first_failing_condition"]) for c in result["not_done"]] == [("zulu", "checks")]
    alpha, zulu = result["companies"]
    assert alpha["markers"] == {"plain_brief": True, "worker_apps": True}
    assert zulu["markers"] == {"plain_brief": False, "worker_apps": False}
    assert zulu["readability"] == {"brief": "system-log", "messages": "dense"}
    assert zulu["seed_status"] == "seeded_review_failed"
    assert read(out / "COHORT.json") == result
    markdown = (out / "COHORT.md").read_text()
    assert "| checks | 1 | 1 | 0 |" in markdown
    assert "| mail | 10 | 8 | 18 |" in markdown
    assert "33.3%" in markdown and "| zulu | checks |" in markdown
    assert "&#124; customer" in markdown
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    cohort_report(cohort, out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_missing_artifacts_are_not_passes_and_do_not_create_evidence(tmp_path):
    companies = tmp_path / "companies"
    folder = companies / "pending"
    folder.mkdir(parents=True)
    result = cohort_report(companies, tmp_path / "out")
    assert result["done"] == 0
    assert all(c == {"passed": 0, "failed": 0, "missing": 1} for c in result["conditions"].values())
    assert [(c["company"], c["first_failing_condition"], c["stage"]) for c in result["not_done"]] == [
        ("pending", "seeded", "awaiting_apps")
    ]
    assert result["stages"] == {"awaiting_apps": 1}
    assert result["calibration"]["pass_rate"] is None
    assert list(folder.iterdir()) == []


def test_empty_cohort_has_no_rate(tmp_path):
    companies = tmp_path / "companies"
    companies.mkdir()
    result = cohort_report(companies, tmp_path / "out")
    assert result["company_count"] == 0 and result["not_done"] == []
    assert result["calibration"]["pass_rate"] is None
    assert result["difficulty"]["distribution"]["total"] == 0
    assert all(r["pass_rate"] is None for r in result["difficulty"]["teacher_by_difficulty"].values())


def test_old_reviews_and_unlisted_calibration_are_included(cohort, tmp_path):
    write(
        cohort / "alpha/world/REVIEW.json",
        {
            "rounds": [
                {"round": 0, "verdict": {"verdict": "accept"}, "receipt": {"model": "fake"}},
                {"round": 1, "verdict": {"verdict": "revise"}, "receipt": {"votes": 3, "accepts": 1}},
            ]
        },
    )
    write(cohort / "alpha/runtime/grades/orphan/calibration.json", {"accepted": True})
    result = cohort_report(cohort, tmp_path / "out")
    assert [r["pattern"] for r in result["review_by_vote_pattern"]] == [
        "1 accept",
        "1 accept / 3 votes",
        "1 accept, 2 revise",
    ]
    assert result["calibration"]["accepted"] == 2


def test_invalid_json_is_an_error_but_a_bad_bulk_count_is_a_counted_anomaly(cohort, tmp_path):
    """One company's arithmetic used to stop the whole 99-company report.

    strive-health's BULK.json claims 2,104 gmail records added to a state holding 409, and 1,107
    drive records to 310. cohort_report raised on the first of those, so the cohort report could
    not be produced at all -- a report whose stated job is to count missing evidence separately.
    """
    path = cohort / "alpha/world/BULK.json"
    path.write_text("broken")
    with pytest.raises(json.JSONDecodeError):
        cohort_report(cohort, tmp_path / "out")
    write(path, {"apps": {"mail": {"added": {"emails": 100}}}})
    result = cohort_report(cohort, tmp_path / "out")
    assert result["record_anomalies"] == [
        {
            "company": "alpha",
            "app": "mail",
            "bulk_added": 100,
            "records_in_state": 9,
            "note": "BULK.json claims more added records than the state holds",
        }
    ]
    # alpha's nine records are all attributed to bulk; zulu's counts are untouched.
    assert result["records_per_app"] == {"mail": {"human": 5, "bulk": 13, "total": 18}}
    assert "Record-count anomalies" in (tmp_path / "out/COHORT.md").read_text()


def test_missing_and_inconsistent_agreements_stay_unfinished(cohort, tmp_path):
    agreement = read(cohort / "alpha/AGREEMENT.json")
    agreement["done"] = False
    agreement["conditions"]["extra"] = {"ok": True}
    write(cohort / "alpha/AGREEMENT.json", agreement)
    result = cohort_report(cohort, tmp_path / "out")
    assert result["not_done"][0]["company"] == "alpha"
    assert result["not_done"][0]["first_failing_condition"] == "done"
    assert result["conditions"]["extra"] == {"passed": 1, "failed": 0, "missing": 1}


def test_difficulty_uses_teacher_classes_and_scores_without_counting_infrastructure_failures(tmp_path):
    folder = tmp_path / "companies/acme"
    cases = [
        ("easy", "teacher_passed", 0.8),
        ("easy", "teacher_failed", 0.2),
        ("easy", "environment_error", 1),
        ("medium", "teacher_failed", 0.9),
        ("hard", "grader_error", 0.99),
        ("hard", None, None),
        (None, "teacher_passed", 1),
    ]
    for index, (difficulty, classification, score) in enumerate(cases):
        write(folder / f"tasks/job_{index}/workflow.json", {"id": f"job_{index}", "difficulty": difficulty})
        if classification:
            write(
                folder / f"runtime/teacher/job_{index}/TEACHER.json",
                {"class": classification, "score": score},
            )
    # Neither a historical attempt nor an orphan result is another task.
    write(folder / "runtime/teacher/job_0/attempt-1/TEACHER.json", {"class": "teacher_failed", "score": 0})
    write(folder / "runtime/teacher/orphan/TEACHER.json", {"class": "teacher_passed", "score": 1})
    write(folder / "tasks/_private/workflow.json", {"difficulty": "easy"})
    result = cohort_report(folder.parent, tmp_path / "out")["difficulty"]
    assert result["distribution"]["counts"] == {"easy": 3, "medium": 1, "hard": 2, "unlabeled": 1}
    easy = result["teacher_by_difficulty"]["easy"]
    assert easy == {
        "tasks": 3,
        "classes": {"environment_error": 1, "teacher_failed": 1, "teacher_passed": 1},
        "evaluated": 2,
        "passed": 1,
        "pass_rate": 0.5,
        "mean_score": 0.5,
        "scored": 2,
        "invalid_scores": 0,
    }
    medium = result["teacher_by_difficulty"]["medium"]
    assert medium["pass_rate"] == 0 and medium["mean_score"] == 0.9
    hard = result["teacher_by_difficulty"]["hard"]
    assert hard["evaluated"] == 0 and hard["pass_rate"] is None and hard["mean_score"] is None
    assert hard["classes"] == {"grader_error": 1, "missing": 1}
    assert result["teacher_by_difficulty"]["unlabeled"]["pass_rate"] == 1
    markdown = (tmp_path / "out/COHORT.md").read_text()
    assert "| easy | 3 | 2 | 1 | 50.0% | 0.500 |" in markdown
    assert "| hard | 2 | 0 | 0 | not yet | not yet |" in markdown


@pytest.mark.parametrize("score", [None, -0.1, 1.1, True, "0.5", float("nan"), float("inf")])
def test_invalid_teacher_scores_are_counted_and_excluded_from_the_average(tmp_path, score):
    folder = tmp_path / "companies/acme"
    write(folder / "tasks/job/workflow.json", {"difficulty": "hard"})
    write(folder / "runtime/teacher/job/TEACHER.json", {"class": "teacher_failed", "score": score})
    result = cohort_report(folder.parent, tmp_path / "out")["difficulty"]["teacher_by_difficulty"]["hard"]
    assert result["invalid_scores"] == 1 and result["scored"] == 0 and result["mean_score"] is None
    assert result["pass_rate"] == 0


def test_unknown_difficulty_in_cohort_is_an_error(tmp_path):
    folder = tmp_path / "companies/acme"
    write(folder / "tasks/job/workflow.json", {"difficulty": "expert"})
    with pytest.raises(ValueError, match="difficulty"):
        cohort_report(folder.parent, tmp_path / "out")


def test_exported_task_uses_backfill_only_while_the_source_matches(tmp_path):
    from company_envs.storage import digest

    folder = tmp_path / "companies/acme"
    workflow = {"id": "job"}
    write(folder / "MANIFEST.json", {"source_run": "source"})
    write(folder / "tasks/job/workflow.json", workflow)
    write(folder / "runtime/teacher/job/TEACHER.json", {"class": "teacher_passed", "score": 1})
    write(
        tmp_path / "runs/source/dataset.json",
        {
            "workflows": [
                {
                    "workflow_id": "job",
                    "verdict": "accept",
                    "difficulty": "hard",
                    "difficulty_input_hash": digest(workflow),
                }
            ]
        },
    )
    result = cohort_report(folder.parent, tmp_path / "out")["difficulty"]
    assert result["teacher_by_difficulty"]["hard"]["pass_rate"] == 1
    write(folder / "tasks/job/workflow.json", {"id": "job", "brief": "Changed work"})
    result = cohort_report(folder.parent, tmp_path / "out")["difficulty"]
    assert result["teacher_by_difficulty"]["unlabeled"]["pass_rate"] == 1
    write(folder / "tasks/job/workflow.json", {"id": "job", "difficulty": "easy"})
    result = cohort_report(folder.parent, tmp_path / "out")["difficulty"]
    assert result["teacher_by_difficulty"]["easy"]["pass_rate"] == 1
