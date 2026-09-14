import json
from types import SimpleNamespace

import pytest
from test_pipeline import setup_job

from company_envs import pipeline
from company_envs.catalogs import Catalogs, sector_targets
from company_envs.models import ModelOutputInvalid
from company_envs.portfolio import company_histograms, summarize
from company_envs.research import research
from company_envs.review import evidence_errors
from company_envs.schemas import Candidate, Company, Discovery
from company_envs.sources import Sources
from company_envs.storage import digest, read, write


class Recorder:
    def __init__(self, company, config=None):
        self.company = company
        self.config = config or {}
        self.calls = []

    def call(self, job, prompt, response_type, **kwargs):
        self.calls.append((job, prompt, json.loads(prompt.rsplit("\n", 1)[1])))
        return self.company.model_copy(deep=True), {"model": "fake/author", "job": job}


def candidate(company):
    return Candidate(
        id=company.id,
        real_firm=company.real_firm,
        sector=company.sector,
        website=company.website,
        reason="Relevant operations",
    )


def add_missing_claim(company, claim_id="details"):
    company.evidence.append(
        company.evidence[0].model_copy(
            update={"id": claim_id, "source_url": f"https://example.com/{claim_id}"}
        )
    )


def capture_double(monkeypatch, *, failed=()):
    def capture(self, url):
        if url in failed:
            return {"status": "unreadable", "error": "HTTPError: 403 Forbidden", "text": ""}
        text = "We manufacture instruments."
        return {"status": "captured", "text": text, "text_hash": digest(text)}

    monkeypatch.setattr(Sources, "capture", capture)


def test_uncapturable_claim_demotes_and_survives_prepare(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    (path / "company.json").unlink()
    state["config"]["design"]["standard_apps"] = []
    add_missing_claim(company)
    recorder = Recorder(company, state["config"])
    monkeypatch.setattr(pipeline, "Models", lambda *args: recorder)
    capture_double(monkeypatch, failed={company.evidence[1].source_url})

    pipeline.prepare(root, directory, state, state["jobs"][company.id])

    assert len(recorder.calls) == 1  # No repair call for the isolated capture gap.
    saved = Company.model_validate(read(path / "company.json"))
    claim = saved.evidence[1]
    assert claim.kind == "inferred"
    assert claim.source_url == company.evidence[1].source_url
    assert "403" in claim.basis
    evidence = read(path / "evidence.json")
    assert evidence[1]["attempted_source_url"] == claim.source_url
    assert evidence[1]["capture_status"] == "unreadable"
    assert evidence[1]["status"] == "declared"
    assert not evidence_errors(saved, evidence)
    assert Sources(root).evidence(saved) == evidence


def test_no_captured_core_page_rejects_dossier(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    (path / "company.json").unlink()
    state["config"]["design"]["standard_apps"] = []
    state["config"]["generation"]["max_revisions"] = 0
    recorder = Recorder(company, state["config"])
    monkeypatch.setattr(pipeline, "Models", lambda *args: recorder)
    capture_double(monkeypatch, failed={company.website})
    with pytest.raises(pipeline.DesignRejected, match="core page"):
        pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert not (path / "company.json").exists()


def test_software_capture_cannot_replace_core_grounding(tmp_path, company, monkeypatch):
    add_missing_claim(company, "software-docs")
    company.software[0].evidence_ids = ["software-docs"]
    capture_double(monkeypatch, failed={company.website})
    evidence = Sources(tmp_path).evidence(company)
    assert evidence[1]["status"] == "supported"
    assert any("core page" in error for error in evidence_errors(company, evidence))


@pytest.mark.parametrize("kind", ["sourced", "inferred"])
def test_captured_quote_mismatch_still_rejects(tmp_path, company, monkeypatch, kind):
    company.evidence[0].kind = kind
    company.evidence[0].basis = "Proposed operating model"
    company.evidence[0].quote = "A fabricated quotation."
    capture_double(monkeypatch)
    evidence = Sources(tmp_path).evidence(company)
    assert company.evidence[0].kind == kind
    assert evidence[0]["status"] == "unverified"
    assert evidence_errors(company, evidence)


def test_mostly_inferred_grounding_flag_excludes_synthetic_choices(tmp_path, company, monkeypatch):
    add_missing_claim(company, "detail-a")
    add_missing_claim(company, "detail-b")
    capture_double(monkeypatch, failed={c.source_url for c in company.evidence[1:]})
    evidence = Sources(tmp_path).evidence(company)
    assert any("mostly inferred" in error for error in evidence_errors(company, evidence))
    company.evidence[2].kind = "synthetic"
    company.evidence[2].source_url = ""
    company.evidence[2].quote = ""
    company.evidence[2].basis = "Synthetic workspace choice"
    assert not evidence_errors(company, Sources(tmp_path).evidence(company))


def test_repair_lists_failed_pages_and_prefers_capturable_sources(root, company):
    recorder = Recorder(company)
    url = "https://example.com/removed"
    feedback = json.dumps(
        {
            "errors": "mostly inferred grounding",
            "source_status": [
                {
                    "kind": "inferred",
                    "attempted_source_url": url,
                    "capture_status": "unreadable",
                    "capture_error": "HTTPError: 404 Not Found",
                }
            ],
        }
    )
    research(
        recorder, Catalogs(root / "catalogs"), "skill", candidate(company), {}, feedback, previous=company
    )
    job, prompt, payload = recorder.calls[0]
    assert job == "research"  # Demoted failures still receive web-enabled repair.
    assert payload["failed_source_pages"] == [
        {"url": url, "capture_status": "unreadable", "capture_error": "HTTPError: 404 Not Found"}
    ]
    assert "homepage, about, careers and press" in prompt


def test_unreadable_hosts_names_only_hosts_that_never_once_produced_text(root):
    """Capture failures drove 70 of 82 research repairs; 34 hosts failed 114 times in a row."""
    pages = {
        "https://dead.test/about": {"status": "unreadable", "error": "HTTPError: 403 Forbidden"},
        "https://dead.test/careers": {"status": "unreadable", "error": "ReadTimeout"},
        # One success clears a host: the next page there may well be readable too.
        "https://mixed.test/a": {"status": "unreadable", "error": "HTTPError: 403 Forbidden"},
        "https://mixed.test/b": {"status": "captured", "error": "", "text": "We manufacture instruments."},
        # A single failure is not yet a pattern, and a blank capture is a failure.
        "https://once.test/a": {"status": "unreadable", "error": "HTTPError: 404 Not Found"},
        "https://blank.test/a": {"status": "captured", "error": "", "text": "   "},
        "https://blank.test/b": {"status": "captured", "error": "", "text": ""},
    }
    for url, page in pages.items():
        write(root / "data" / "sources" / f"{digest(url)}.json", {"url": url, **page})
    assert pipeline.unreadable_hosts(root) == ["blank.test", "dead.test"]
    assert pipeline.unreadable_hosts(root, minimum=1) == ["blank.test", "dead.test", "once.test"]
    assert pipeline.unreadable_hosts(root, minimum=3) == []


def with_apps(company, company_id, apps, sector=None):
    result = company.model_copy(deep=True, update={"id": company_id, "sector": sector or company.sector})
    result.software[0].catalog_app_ids = apps
    result.software[0].status = "substitution"
    return result


def test_summary_counts_companies_not_tasks_and_includes_zeroes(company, workflow):
    first = with_apps(company, "first", ["gmail_mock", "slack_mock", "off_surface"])
    first.software.append(first.software[0].model_copy(update={"id": "duplicate-apps"}))
    second = with_apps(company, "second", ["gmail_mock"], "Retail")
    workflows = [workflow.model_copy(update={"id": f"w{i}", "company_id": "first"}) for i in range(5)]
    counters = company_histograms(
        [first, second, first],
        surface=["gmail_mock", "slack_mock", "asana_mock"],
        sectors=["Manufacturing", "Retail", "Unused"],
    )
    assert counters["app_usage"] == {"asana_mock": 0, "gmail_mock": 2, "slack_mock": 1}
    assert counters["sector_counts"] == {"Manufacturing": 1, "Retail": 1, "Unused": 0}
    # The bounded prose sample stands beside those counters rather than repeating them.
    result = summarize(workflows, limit=0)
    assert result["shown_workflows"] == 0
    assert result["total_workflows"] == 5
    assert not set(counters) & set(result)


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"gmail_mock": 3, "slack_mock": 1}, ["asana_mock"]),
        ({"gmail_mock": 3, "slack_mock": 1, "asana_mock": 1}, ["asana_mock", "slack_mock"]),
        ({}, ["asana_mock", "gmail_mock", "slack_mock"]),
    ],
)
def test_research_under_used_apps_reflect_histogram(company, usage, expected):
    surface = ["gmail_mock", "slack_mock", "asana_mock"]
    company = with_apps(company, company.id, ["gmail_mock"])
    recorder = Recorder(
        company, {"design": {"available_runtime_apps": surface, "standard_apps": ["gmail_mock"]}}
    )
    catalogs = SimpleNamespace(
        apps=dict.fromkeys([*surface, "off_surface"]),
        occupations={"17-2112": "Industrial Engineer"},
        economic_context=lambda sector: {},
        software_context=lambda surface: [],
    )
    research(recorder, catalogs, "skill", candidate(company), {"app_usage": usage})
    payload = recorder.calls[0][2]
    assert payload["under_used_apps"] == expected
    assert payload["standard_apps"] == ["gmail_mock"]
    assert "Never invent adoption" in payload["app_selection_instruction"]
    company.software[0].catalog_app_ids = ["asana_mock"]
    with pytest.raises(ModelOutputInvalid, match="mandatory standard apps"):
        research(recorder, catalogs, "skill", candidate(company), {"app_usage": usage})


def test_coverage_counts_all_accepted_companies_outside_selected_quota(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["design"]["available_runtime_apps"] = ["gmail_mock", "slack_mock"]
    state["config"]["design"]["feature_matrix"] = True
    accepted = with_apps(company, company.id, ["gmail_mock"])
    rejected = with_apps(company, "rejected", ["slack_mock"])
    for dossier in [accepted, rejected]:
        relative = f"dossiers/{dossier.id}.json"
        write(root / relative, dossier.model_dump())
        state["companies"][dossier.id] = relative
    relative = str((path / "one-workflow.json").relative_to(root))
    write(root / relative, workflow.model_dump())
    state["entries"] = [
        {"company_id": company.id, "verdict": "accept", "workflow_path": relative},
        {"company_id": company.id, "verdict": "accept", "workflow_path": relative},
        {"company_id": "rejected", "verdict": "reject", "workflow_path": relative},
    ]
    monkeypatch.setattr(pipeline, "chosen", lambda *args: [])
    cov = pipeline.coverage(root, state, Catalogs(directory / "catalogs"))
    assert cov["app_usage"] == {"gmail_mock": 1, "slack_mock": 0}
    assert not {"app_usage", "sector_counts", "assigned_app_history"} & set(cov["portfolio"])
    assert cov["sector_counts"][company.sector] == 1
    assert sum(cov["sector_counts"].values()) == 1
    assert cov["companies"] == 0  # Selection counts retain their existing meaning.


def test_discovery_payload_carries_sector_deficits(root, monkeypatch):
    directory = pipeline.new_run(root, 20, 20)
    state = read(directory / "run.json")
    catalogs = Catalogs(directory / "catalogs")
    targets = sector_targets(catalogs.sectors, 20)
    sector = max(targets, key=targets.get)
    state["jobs"]["queued"] = {
        "id": "queued",
        "status": "queued",
        "candidate": {"sector": sector, "real_firm": "Already queued firm"},
    }
    cov = {"companies": 0, "sectors": {}, "sector_counts": {s: 0 for s in targets}, "app_usage": {}}
    monkeypatch.setattr(pipeline, "coverage", lambda *args: cov)
    recorder = Recorder(Discovery(candidates=[]), state["config"])
    monkeypatch.setattr(pipeline, "Models", lambda *args: recorder)
    assert not pipeline.fill_queue(root, directory, state, catalogs)
    job, _, payload = recorder.calls[0]
    expected = {s: max(0, count - (s == sector)) for s, count in targets.items()}
    assert job == "discover"
    assert payload["sector_deficits"] == expected
    assert payload["coverage"]["sector_deficits"] == expected
    assert payload["sector_counts"] == cov["sector_counts"]
    assert "under-covered sectors" in payload["sector_selection_instruction"]
