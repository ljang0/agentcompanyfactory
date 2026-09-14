"""Model-free assignment, call validation and coordinator reservation coverage."""

import json
import tomllib
from pathlib import Path

import pytest
from test_pipeline import Critic, setup_job

from company_envs import pipeline
from company_envs.app_affinity import (
    COMMS_APPS,
    HR_APPS,
    REGIONAL_APPS,
    affinity_table,
    assign_apps,
    eligible_scores,
)
from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid
from company_envs.portfolio import (
    assigned_app_history,
    company_histograms,
    freeze_history,
    history_app_context,
    in_flight_app_usage,
    summarize,
)
from company_envs.research import research
from company_envs.schemas import Candidate, Company, Discovery
from company_envs.storage import read, write

REPO = Path(__file__).resolve().parents[1]


def candidate(company, **updates):
    return Candidate(
        **dict(
            id=company.id,
            real_firm=company.real_firm,
            website=company.website,
            sector=company.sector,
            reason="Captured operating evidence",
            **updates,
        )
    )


class Author:
    def __init__(self, company, surface, mode="adopt"):
        self.company = company
        self.config = {
            "generation": {"seed": 19},
            "design": {"available_runtime_apps": surface, "standard_apps": ["gmail_mock"]},
        }
        self.mode = mode
        self.payloads = []

    def call(self, job, prompt, response_type, **kwargs):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        raw = self.company.model_dump()
        assigned = payload["assigned_apps"]
        apps = ["gmail_mock"]
        if self.mode in ("adopt", "candidate", "blank"):
            apps.append(assigned[0])
        if self.mode == "sheets":
            apps.append("google_sheets_mock")
        raw["software"][0].update(
            catalog_app_ids=apps,
            status="catalog_candidate" if self.mode == "candidate" else "substitution",
            capability="Production release records",
            rationale="Engineering tracks release decisions for instruments.",
        )
        if self.mode == "blank":
            raw["software"][0]["rationale"] = " "
        raw["declined_apps"] = [
            {"app_id": app, "reason": "This unit has no corresponding operational requirement."}
            for app in assigned
            if self.mode in ("decline", "sheets", "partial", "blank-reason")
        ]
        if self.mode == "partial":
            raw["declined_apps"].pop()
        if self.mode == "blank-reason":
            raw["declined_apps"][0]["reason"] = " "
        if self.mode == "contradictory":
            raw["software"][0]["catalog_app_ids"].append(assigned[0])
            raw["declined_apps"] = [{"app_id": assigned[0], "reason": "Not needed."}]
        assert "declined_apps" in kwargs["schema"]["required"]
        return response_type.model_validate(raw), {"model": "fake/author", "job": job}


@pytest.fixture
def catalogs(root):
    return Catalogs(root / "catalogs")


def test_every_surface_app_and_gdpval_sector_has_affinity(catalogs):
    configured = tomllib.loads((REPO / "config.toml").read_text())["design"]["available_runtime_apps"]
    table = affinity_table(catalogs, configured)
    assert set(table) == set(configured)
    # 98 of 98: every hub clone with a pinned schema document. It was 88 until the last ten were
    # ported on 2026-09-10 -- four had no catalogue row, two had state_keys: [], four had schema: ""
    # -- and each of the ten is on the surface only because it passes hub-smoke with its fixture.
    assert len(table) == 98
    sectors = {sector["name"] for sector in catalogs.sectors}
    assert all(set(scores) == sectors and max(scores.values()) > 0 for scores in table.values())
    assert table == affinity_table(catalogs, list(reversed(configured)))
    assert all(min(table[f"{app}_mock"].values()) > 0 for app in HR_APPS)
    assert all(max(table[f"{app}_mock"].values()) == 1 for app in COMMS_APPS)


def test_affinity_reads_names_notes_and_only_first_sixty_schema_lines(catalogs):
    app = catalogs.apps["zillow_mock"]
    path = catalogs.path / "app_schemas" / Path(app["schema"]).name
    app["name"], app["notes"] = "Patient", "radiology"
    path.write_text("medical\n" + "\n" * 59 + "clinical\n")
    score = affinity_table(catalogs, [app["id"]])[app["id"]]["Health Care and Social Assistance"]
    assert score == 12  # Each of three bounded keywords contributes four.
    path.write_text("medical\nclinical\n")
    assert affinity_table(catalogs, [app["id"]])[app["id"]]["Health Care and Social Assistance"] == 16


@pytest.mark.parametrize(
    "sector,expected",
    [
        ("Health Care and Social Assistance", {"epic-health_mock", "PACS-viewer_mock"}),
        ("Information", {"github_mock", "gitlab_mock"}),
        ("Manufacturing", {"SAP_mock", "ServiceNow_mock"}),
        ("Real Estate and Rental and Leasing", {"zillow_mock", "docusign_mock"}),
    ],
)
def test_equal_usage_respects_sector(catalogs, company, sector, expected):
    firm = candidate(company).model_copy(update={"sector": sector})
    surface = list(expected | {"robinhood_mock", "gmail_mock", "slack_mock"})
    selected = assign_apps(catalogs, firm, {}, seed=42, surface=surface)
    assert set(selected) == expected
    assert selected == assign_apps(catalogs, firm, {}, seed=42, surface=list(reversed(surface)))


def test_unused_before_affinity_and_reservations_before_tie_break(catalogs, company):
    surface = ["SAP_mock", "ServiceNow_mock", "workday_mock", "bamboohr_mock"]
    cov = {"app_usage": {"SAP_mock": 3, "ServiceNow_mock": 1}, "in_flight_app_usage": {"workday_mock": 1}}
    selected = assign_apps(catalogs, candidate(company), cov, surface=surface)
    assert selected[0] == "bamboohr_mock"
    assert selected[1] == "ServiceNow_mock"
    assert (
        assign_apps(
            catalogs, candidate(company), {"portfolio": {"app_usage": {"SAP_mock": 5}}}, surface=surface
        )[0]
        != "SAP_mock"
    )


def test_standard_apps_never_assigned_and_sheets_at_most_once(catalogs, company):
    selected = assign_apps(
        catalogs,
        candidate(company),
        {},
        surface=["gmail_mock", "slack_mock", "SAP_mock", "google_sheets_mock", "google_sheets_mock"],
        standard_apps=["SAP_mock"],
    )
    assert selected == ["google_sheets_mock"]


@pytest.mark.parametrize("status,expected", [("captured", True), ("unreadable", False)])
def test_regional_apps_require_captured_market_evidence(catalogs, company, status, expected):
    firm = candidate(company).model_copy(update={"sector": "Retail Trade", "reason": "China operation"})
    surface = [f"{app}_mock" for app in REGIONAL_APPS]
    assert assign_apps(catalogs, firm, {}, surface=surface) == []
    excerpts = [{"url": firm.website, "capture_status": status, "excerpt": "We operate stores in China."}]
    assert bool(assign_apps(catalogs, firm, {}, surface=surface, excerpts=excerpts)) is expected
    assert not assign_apps(
        catalogs, firm, {}, surface=surface, excerpts=[{**excerpts[0], "capture_error": "403"}]
    )


def test_captured_communication_preference_increases_affinity(catalogs):
    app = "microsoft_teams_mock"
    scores = eligible_scores(
        catalogs,
        "Manufacturing",
        [app],
        [
            {
                "url": "https://example.test",
                "excerpt": "Our staff use Microsoft Teams.",
                "capture_status": "captured",
            }
        ],
    )
    assert scores[app] == 30


@pytest.mark.parametrize("mode", ["adopt", "decline"])
def test_research_adoption_or_complete_decline_roundtrips(catalogs, company, mode):
    author = Author(company, ["SAP_mock", "ServiceNow_mock", "gmail_mock"], mode)
    dossier, receipt = research(author, catalogs, "skill", candidate(company), {})
    assert type(dossier) is Company
    assert Company.model_validate(dossier.model_dump()) == dossier
    decision = receipt["app_assignment"]
    assert len(decision["assigned_apps"]) == 2
    assert bool(decision["adopted_apps"]) is (mode == "adopt")
    assert len(decision["declined_apps"]) == (2 if mode == "decline" else 0)
    # A repair uses the same coordinator assignment even if coverage or seed changes.
    fixed = author.payloads[0]["assigned_apps"]
    author.config["generation"]["seed"] = 900
    research(author, catalogs, "skill", candidate(company), {"assigned_apps": fixed}, previous=dossier)
    assert author.payloads[-1]["assigned_apps"] == fixed


@pytest.mark.parametrize(
    "mode,match",
    [
        ("missing", "Adopt at least one"),
        ("partial", "Adopt at least one"),
        ("candidate", "Adopt at least one"),
        ("blank", "Adopt at least one"),
        ("blank-reason", "nonblank"),
        ("contradictory", "not included"),
        ("sheets", "Sheets-only"),
    ],
)
def test_research_invalid_selection_requests_bounded_repair(catalogs, company, mode, match):
    author = Author(company, ["SAP_mock", "ServiceNow_mock", "gmail_mock", "google_sheets_mock"], mode)
    with pytest.raises(ModelOutputInvalid, match=match) as error:
        research(author, catalogs, "skill", candidate(company), {})
    assert error.value.receipt["model"] == "fake/author"
    assert "declined_apps" in error.value.raw
    if mode == "sheets":
        assert all(app in str(error.value) for app in author.payloads[0]["assigned_apps"])


def test_queue_reserves_distinct_apps_and_discovery_gets_categories(root, company, monkeypatch):
    directory = pipeline.new_run(root, 4, 4)
    state = read(directory / "run.json")
    state["config"]["design"]["available_runtime_apps"] = [
        "SAP_mock",
        "ServiceNow_mock",
        "monday_mock",
        "lucidchart_mock",
        "tableau_mock",
        "workday_mock",
    ]
    state["config"]["design"]["standard_apps"] = []
    monkeypatch.setattr(pipeline, "sector_targets", lambda *args: {"Manufacturing": 4})
    payloads = []

    class Discoverer:
        def __init__(self, config, directory):
            self.config = config

        def call(self, job, prompt, response_type, **kwargs):
            payloads.append(json.loads(prompt.rsplit("\n", 1)[1]))
            return Discovery(
                candidates=[
                    candidate(company).model_copy(
                        update={
                            "id": f"firm-{i}",
                            "real_firm": f"Firm {i}",
                            "website": f"https://firm{i}.test",
                        }
                    )
                    for i in range(3)
                ]
            ), {"job": job}

    monkeypatch.setattr(pipeline, "Models", Discoverer)
    catalogs = Catalogs(directory / "catalogs")
    assert pipeline.fill_queue(root, directory, state, catalogs)
    assignments = [job["coverage"]["assigned_apps"] for job in state["jobs"].values()]
    assert len({app for apps in assignments for app in apps}) == 6
    assert payloads[0]["under_used_app_categories"]
    cov = pipeline.coverage(root, state, catalogs)
    assert sum(cov["app_usage"].values()) == 0
    assert sum(cov["in_flight_app_usage"].values()) == 6
    assert len(cov["assigned_app_history"]) == 3
    first = next(iter(state["jobs"].values()))
    first["status"] = "rejected"
    assert sum(in_flight_app_usage(state["jobs"]).values()) == 4
    assert len(assigned_app_history(state["jobs"])) == 3
    assert sum(in_flight_app_usage(state["jobs"], {"firm-1"}).values()) == 2


def test_portfolio_freezes_company_usage_and_assignment_decisions(root, company, workflow, monkeypatch):
    company.software[0].catalog_app_ids = ["SAP_mock"]
    company.software[0].status = "substitution"
    directory, state, path = setup_job(root, company, workflow)
    state["jobs"][company.id]["coverage"]["assigned_apps"] = ["SAP_mock", "ServiceNow_mock"]
    authors = read(path / "authors.json")
    authors["receipts"][0]["app_assignment"] = {
        "company_id": company.id,
        "assigned_apps": ["SAP_mock", "ServiceNow_mock"],
        "adopted_apps": ["SAP_mock"],
        "declined_apps": {},
    }
    write(path / "authors.json", authors)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    write(directory / "run.json", state)
    future = root / "runs" / "future"
    manifest = freeze_history(root, future)
    companies, history = history_app_context(future, {"portfolio": manifest})
    assert [c.id for c in companies] == [company.id]
    assert history[0]["adopted_apps"] == ["SAP_mock"]
    counters = company_histograms(companies * 2, surface=["SAP_mock", "ServiceNow_mock"])
    assert counters["app_usage"] == {"SAP_mock": 1, "ServiceNow_mock": 0}
    assert "assigned_app_history" not in summarize([], limit=0)
    future_state = {
        **state,
        "id": "future",
        "portfolio": manifest,
        "entries": [],
        "companies": {},
        "jobs": {},
    }
    future_state["config"]["design"]["available_runtime_apps"] = ["SAP_mock", "ServiceNow_mock"]
    cov = pipeline.coverage(root, future_state, Catalogs(root / "catalogs"))
    assert cov["app_usage"] == counters["app_usage"]
    assert "app_usage" not in cov["portfolio"]
    assert cov["assigned_app_history"] == history
    # Live prior runs are irrelevant once the next run freezes its portfolio.
    write(directory / "run.json", {"changed": True})
    assert history_app_context(future, {"portfolio": manifest}) == (companies, history)


def test_drafted_apps_and_reservations_count_once_and_accepted_is_not_double_counted(tmp_path, company):
    company.software[0].catalog_app_ids = ["SAP_mock", "tableau_mock"]
    company.software[0].status = "substitution"
    jobs = {
        company.id: {
            "id": company.id,
            "candidate": candidate(company).model_dump(),
            "status": "drafted",
            "coverage": {"assigned_apps": ["SAP_mock", "ServiceNow_mock"]},
        }
    }
    write(tmp_path / "jobs" / company.id / "company.json", company.model_dump())
    assert in_flight_app_usage(jobs, run_dir=tmp_path) == {
        "SAP_mock": 1,
        "ServiceNow_mock": 1,
        "tableau_mock": 1,
    }
    assert in_flight_app_usage(jobs, {company.id}, tmp_path) == {}
