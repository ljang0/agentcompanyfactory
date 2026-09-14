"""Real-firm coverage and app evidence use local catalog and dossier doubles."""

import typing

from company_envs.app_affinity import app_coverage, assign_apps, candidate_app_evidence, target_apps
from company_envs.catalogs import Catalogs
from company_envs.research import AppCandidate
from company_envs.storage import write


def test_coverage_unions_versions_and_jobs_by_real_firm(root, company):
    catalogs = Catalogs(root / "catalogs")
    surface = ["SAP_mock", "jira_mock", "twitter_mock", "reddit_mock", "12306_mock"]
    state = {"id": "current", "config": {"design": {"available_runtime_apps": surface}}, "jobs": {}}
    for index, (firm, apps) in enumerate(
        [
            ("Acme, Inc.", ["SAP_mock"]),
            ("ACME", ["SAP_mock", "jira_mock"]),
            ("Second Firm", ["SAP_mock"]),
        ]
    ):
        raw = company.model_dump()
        raw.update(id=f"version-{index}", real_firm=firm)
        raw["software"][0]["catalog_app_ids"] = apps
        write(root / "data" / "companies" / raw["id"] / "accepted.json", raw)
    for job_id, firm, status in [
        ("one", "Acme Inc.", "queued"),
        ("two", "ACME", "drafted"),
        ("bad", "Other", "rejected"),
    ]:
        state["jobs"][job_id] = {
            "candidate": {"id": job_id, "real_firm": firm},
            "status": status,
            "coverage": {"assigned_apps": ["twitter_mock"] if status != "rejected" else ["12306_mock"]},
        }
    raw = company.model_dump()
    raw["software"][0]["catalog_app_ids"] = ["reddit_mock"]
    write(root / "runs" / "current" / "jobs" / "two" / "company.json", raw)
    result = app_coverage(root, state, catalogs)
    assert result["uncovered_apps"] == ["12306_mock"]
    assert result["app_coverage"]["SAP_mock"] == {"accepted_firms": 2, "run_firms": 0}
    assert result["app_coverage"]["jira_mock"]["accepted_firms"] == 1
    assert result["app_coverage"]["twitter_mock"] == {"accepted_firms": 0, "run_firms": 1}
    assert result["app_coverage"]["reddit_mock"]["run_firms"] == 1
    # A finished job releases assignments that never entered its dossier.
    state["jobs"]["one"]["status"] = "reviewed"
    state["jobs"]["two"]["status"] = "reviewed"
    assert app_coverage(root, state, catalogs)["uncovered_apps"] == ["12306_mock", "twitter_mock"]


def test_rotation_covers_tail_and_keeps_progress_when_uncovered_set_shrinks():
    apps = [f"app-{index:02}" for index in range(29)]
    requests = {}
    first = target_apps(apps, requests)
    assert len(first) == 12
    requests.update(dict.fromkeys(first, 1))
    remaining = apps[7:]
    second = target_apps(remaining, requests)
    assert not set(first) & set(second)
    requests.update(dict.fromkeys(second, 1))
    third = target_apps(remaining, requests)
    assert set(first + second + third) == set(apps)
    assert target_apps([], requests) == []
    assert target_apps(list(reversed(apps)), {}) == first


def test_cited_app_survives_usage_and_sector_priors(root, company):
    catalogs = Catalogs(root / "catalogs")
    candidate = AppCandidate(
        id=company.id,
        real_firm=company.real_firm,
        sector=company.sector,
        website=company.website,
        reason="Tool adoption cited by discovery",
        app_evidence=[
            {
                "app_id": "12306_mock",
                "source_url": "https://firm.test/travel",
                "quote": "Our travel desk books rail tickets through 12306.cn.",
            }
        ],
    )
    surface = ["12306_mock", "SAP_mock", "ServiceNow_mock"]
    assigned = assign_apps(catalogs, candidate, {"app_usage": {"12306_mock": 99}}, surface=surface)
    assert assigned[0] == "12306_mock"
    assert len(assigned) == 2
    candidate.app_evidence[0].source_url = ""
    assert candidate_app_evidence(catalogs, candidate, surface) == []
    assert "12306_mock" not in assign_apps(catalogs, candidate, {}, surface=surface)


def test_ambiguous_product_names_need_their_own_spelling():
    from company_envs.app_affinity import mentions_app

    class Catalogs:
        apps: typing.ClassVar = {
            "monday_mock": {"name": "Monday (mock)"},
            "12306_mock": {"name": "12306 (mock)"},
            "jira_mock": {"name": "Jira (mock)"},
        }

    c = Catalogs()
    assert not mentions_app(c, "monday_mock", "Hours: Monday through Friday, 9 to 5.")
    assert mentions_app(c, "monday_mock", "We run sprints in monday.com boards.")
    assert not mentions_app(c, "12306_mock", "Schenectady, NY 12306")
    assert mentions_app(c, "jira_mock", "Issues are tracked in Jira.")
