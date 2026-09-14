"""Discovery, source capture and reporting are exercised without network or models."""

import json
from collections import Counter

import pytest
from test_pipeline import Critic, setup_job

from company_envs import pipeline
from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid
from company_envs.report import export
from company_envs.research import AppCandidate, AppDiscovery, capture_app_evidence, discover, research
from company_envs.review import evidence_errors
from company_envs.sources import Sources
from company_envs.storage import digest, read, write


def app_candidate(company, **evidence):
    return AppCandidate(
        id=company.id,
        real_firm=company.real_firm,
        sector=company.sector,
        website=company.website,
        reason="Public adoption evidence",
        app_evidence=[
            {
                "app_id": "SAP_mock",
                "source_url": "https://firm.test/careers",
                "quote": "Our production team uses SAP.",
                **evidence,
            }
        ],
    )


class Recorder:
    def __init__(self, result, config=None):
        self.result = result
        self.config = config or {"design": {"available_runtime_apps": ["SAP_mock", "ServiceNow_mock"]}}
        self.payloads = []

    def call(self, job, prompt, response_type, **kwargs):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        return response_type.model_validate(self.result.model_dump()), {"job": job, "model": "double"}


@pytest.mark.parametrize(
    "row,kept",
    [
        ({}, True),
        ({"source_url": ""}, False),
        ({"source_url": "file:///tmp/page"}, False),
        ({"source_url": "https://user:secret@firm.test/page"}, False),
        ({"quote": "We make instruments."}, False),
        ({"quote": "We hire ASAP."}, False),
        ({"app_id": "unknown_mock"}, False),
        ({"app_id": "ServiceNow_mock", "quote": "We use ServiceNow."}, False),
    ],
)
def test_discovery_requires_cited_target_tool_and_keeps_ordinary_firm(root, company, row, kept):
    recorder = Recorder(AppDiscovery(candidates=[app_candidate(company, **row)]))
    result, _ = discover(
        recorder, Catalogs(root / "catalogs"), "skill", company.sector, [], {"target_apps": ["SAP_mock"]}, 1
    )
    assert result.candidates[0].real_firm == company.real_firm
    assert bool(result.candidates[0].app_evidence) is kept
    payload = recorder.payloads[0]
    assert payload["target_apps"] == ["SAP_mock"]
    instruction = payload["target_app_instruction"]
    for word in (
        "careers",
        "engineering blog",
        "case study",
        "integration",
        "Never invent companies",
        "Personal employee use is insufficient",
    ):
        assert word in instruction
    for app in [
        "twitter",
        "instagram",
        "pinterest",
        "reddit",
        "weibo",
        "wechat",
        "xiaohongshu",
        "zhihu",
        "taobao_seller",
        "12306",
        "uber_eats",
        "instacart",
        "coinbase",
        "robinhood",
        "ebay",
        "facebook",
        "google_flights",
        "booking_com",
        "amazon",
    ]:
        assert app in instruction


@pytest.fixture
def take_discovery_batches():
    def take(recorder):
        # Workers append in scheduling order; fill_queue reserves targets in sector order.
        payloads = sorted(recorder.payloads, key=lambda row: row["sector"])
        recorder.payloads.clear()
        return [row["target_apps"] for row in payloads]

    return take


def test_parallel_discovery_rotates_and_resume_keeps_request_counts(
    root, monkeypatch, take_discovery_batches
):
    directory = pipeline.new_run(root, 3, 3)
    state = read(directory / "run.json")
    state["config"]["generation"]["discovery_parallel"] = 3
    catalogs = Catalogs(directory / "catalogs")
    surface = sorted(state["config"]["design"]["available_runtime_apps"])[:29]
    state["config"]["design"]["available_runtime_apps"] = surface
    sectors = [row["name"] for row in catalogs.sectors[:3]]
    monkeypatch.setattr(pipeline, "sector_targets", lambda *args: dict.fromkeys(sectors, 1))
    recorder = Recorder(AppDiscovery(candidates=[]), state["config"])
    monkeypatch.setattr(pipeline, "Models", lambda *args: recorder)
    assert not pipeline.fill_queue(root, directory, state, catalogs)
    batches = take_discovery_batches(recorder)
    assert len(batches) == 3
    assert all(len(batch) <= 12 for batch in batches)
    assert set().union(*map(set, batches)) == set(surface)
    assert len(set(batches[0]) & set(batches[1])) == 0
    resumed = read(directory / "run.json")
    assert resumed["discovery_app_requests"] == dict(Counter(app for batch in batches for app in batch))
    least_requested = {app for app, count in resumed["discovery_app_requests"].items() if count == 1}
    assert not pipeline.fill_queue(root, directory, resumed, catalogs)
    resumed_batches = take_discovery_batches(recorder)
    assert len(resumed_batches) == 3
    assert set(resumed_batches[0]) <= least_requested


@pytest.mark.parametrize(
    "status,text,kind",
    [
        ("captured", "Our production team uses SAP.", "sourced"),
        ("unreadable", "", "inferred"),
        ("captured", "We make instruments and hire production engineers.", "inferred"),
    ],
)
def test_research_captures_keeps_and_demotes_app_evidence(root, company, monkeypatch, status, text, kind):
    candidate = app_candidate(company)
    catalogs = Catalogs(root / "catalogs")
    captured = []

    def capture(self, url):
        captured.append(url)
        is_app = url == candidate.app_evidence[0].source_url
        content = text if is_app else "We manufacture instruments."
        return {
            "url": url,
            "status": status if is_app else "captured",
            "text": content,
            "text_hash": digest(content),
            "error": "403 Forbidden" if is_app and status == "unreadable" else "",
        }

    monkeypatch.setattr(Sources, "capture", capture)
    sources = Sources(root)
    excerpts = capture_app_evidence(sources, candidate, catalogs)
    assert captured == [candidate.app_evidence[0].source_url]
    company.software[0].catalog_app_ids = ["SAP_mock"]
    company.software[0].status = "catalog_candidate"
    recorder = Recorder(company)
    dossier, receipt = research(
        recorder,
        catalogs,
        "skill",
        candidate,
        {"assigned_apps": ["ServiceNow_mock"], "target_apps": ["SAP_mock"]},
        excerpts=excerpts,
    )
    manifest = sources.evidence(dossier)
    claim = next(c for c in dossier.evidence if c.id in dossier.software[0].evidence_ids)
    assert claim.kind == kind
    assert claim.source_url == candidate.app_evidence[0].source_url
    assert not evidence_errors(dossier, manifest)
    assert receipt["app_assignment"]["adopted_apps"] == ["SAP_mock"]
    assert recorder.payloads[0]["target_apps"] == ["SAP_mock"]
    assert recorder.payloads[0]["captured_app_pages"] == excerpts
    if kind == "inferred":
        assert "unverified" in claim.basis
        assert claim.quote == ""
        assert manifest[-1]["attempted_source_url"] == claim.source_url


def test_evidenced_app_cannot_be_dropped(root, company):
    with pytest.raises(ModelOutputInvalid, match="Keep the candidate's evidenced app"):
        research(Recorder(company), Catalogs(root / "catalogs"), "skill", app_candidate(company), {})


def test_sourced_standard_app_does_not_become_a_domain_assignment(root, company):
    candidate = app_candidate(company, app_id="slack_mock", quote="Our team uses Slack.")
    company.software[0].catalog_app_ids = ["slack_mock"]
    company.software[0].status = "substitution"
    recorder = Recorder(
        company, {"design": {"available_runtime_apps": ["slack_mock"], "standard_apps": ["slack_mock"]}}
    )
    page = {
        "url": candidate.app_evidence[0].source_url,
        "capture_status": "captured",
        "excerpt": candidate.app_evidence[0].quote,
    }
    dossier, _ = research(recorder, Catalogs(root / "catalogs"), "skill", candidate, {}, excerpts=[page])
    assert recorder.payloads[0]["assigned_apps"] == []
    assert dossier.evidence[-1].kind == "sourced"


@pytest.mark.parametrize("status", ["captured", "unreadable"])
def test_prepare_retains_candidate_evidence_in_saved_dossier(root, company, workflow, monkeypatch, status):
    company.software[0].catalog_app_ids = ["SAP_mock"]
    company.software[0].status = "catalog_candidate"
    directory, state, path = setup_job(root, company, workflow)
    candidate = app_candidate(company)
    state["jobs"][company.id]["candidate"] = candidate.model_dump()
    (path / "company.json").unlink()
    recorder = Recorder(company)
    monkeypatch.setattr(pipeline, "Models", lambda *args: recorder)
    captured = []

    def capture(self, url):
        captured.append(url)
        text = "Our production team uses SAP." if url.endswith("careers") else "We manufacture instruments."
        page = {
            "url": url,
            "text": text,
            "text_hash": digest(text),
            "status": status if url.endswith("careers") else "captured",
        }
        write(self.path / f"{digest(url)}.json", page)
        return page

    monkeypatch.setattr(Sources, "capture", capture)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    dossier = read(path / "company.json")
    claim_id = dossier["software"][0]["evidence_ids"][0]
    claim = next(row for row in dossier["evidence"] if row["id"] == claim_id)
    assert claim["kind"] == ("sourced" if status == "captured" else "inferred")
    assert candidate.app_evidence[0].source_url in captured
    assert read(path / "evidence.json")[-1]["kind"] == claim["kind"]
    assert recorder.payloads[0]["candidate"]["app_evidence"] == candidate.model_dump()["app_evidence"]
    # A resumed prepared job reuses its persisted dossier without another author call.
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert len(recorder.payloads) == 1


def test_two_apps_on_one_page_keep_separate_claims_and_core_identity(root, company):
    candidate = app_candidate(company, source_url=company.website, quote="Our team uses SAP and ServiceNow.")
    candidate.app_evidence.append(candidate.app_evidence[0].model_copy(update={"app_id": "ServiceNow_mock"}))
    company.software[0].catalog_app_ids = ["SAP_mock", "ServiceNow_mock"]
    company.software[0].status = "catalog_candidate"
    company.software[0].evidence_ids = ["operations"]
    page = {
        "url": company.website,
        "capture_status": "captured",
        "excerpt": "We manufacture instruments. Our team uses SAP and ServiceNow.",
    }
    dossier, _ = research(
        Recorder(company), Catalogs(root / "catalogs"), "skill", candidate, {}, excerpts=[page]
    )
    assert len(dossier.evidence) == 3
    assert next(c for c in dossier.evidence if c.id == "operations").text == "Makes instruments."
    assert {c.text for c in dossier.evidence if c.id != "operations"} == {
        f"{company.real_firm} uses SAP.",
        f"{company.real_firm} uses ServiceNow.",
    }


def test_missing_tool_demotes_authored_software_claim_without_adding_a_duplicate(root, company):
    candidate = app_candidate(company)
    company.software[0].catalog_app_ids = ["SAP_mock"]
    company.software[0].status = "catalog_candidate"
    claim = company.evidence[0].model_copy(
        update={
            "id": "software-use",
            "text": "The firm uses SAP.",
            "source_url": candidate.app_evidence[0].source_url,
            "quote": candidate.app_evidence[0].quote,
        }
    )
    company.evidence.append(claim)
    company.software[0].evidence_ids = [claim.id]
    page = {"url": claim.source_url, "capture_status": "captured", "excerpt": "The firm makes instruments."}
    dossier, _ = research(
        Recorder(company), Catalogs(root / "catalogs"), "skill", candidate, {}, excerpts=[page]
    )
    assert len(dossier.evidence) == 2
    assert dossier.evidence[-1].kind == "inferred"
    assert dossier.evidence[-1].quote == ""


def test_capture_bounds_large_shared_pages_and_keeps_tool_quote(root, company, monkeypatch):
    candidate = app_candidate(company)
    candidate.app_evidence.append(
        candidate.app_evidence[0].model_copy(
            update={"app_id": "ServiceNow_mock", "quote": "Our help desk uses ServiceNow."}
        )
    )
    calls = []
    text = "Ordinary page content. " * 3000 + "Our production team uses SAP. Our help desk uses ServiceNow."

    def capture(self, url):
        calls.append(url)
        return {"text": text, "status": "captured"}

    monkeypatch.setattr(Sources, "capture", capture)
    pages = capture_app_evidence(Sources(root), candidate, Catalogs(root / "catalogs"))
    assert calls == [candidate.app_evidence[0].source_url]
    assert len(pages) == 1 and len(pages[0]["excerpt"]) <= Sources.PAGE_BUDGET
    assert all(row.quote in pages[0]["excerpt"] for row in candidate.app_evidence)


def test_quote_mismatch_requires_correction_then_accepts_authored_quote(root, company):
    catalogs = Catalogs(root / "catalogs")
    candidate = app_candidate(company)
    company.software[0].catalog_app_ids = ["SAP_mock"]
    company.software[0].status = "catalog_candidate"
    page = {
        "url": candidate.app_evidence[0].source_url,
        "capture_status": "captured",
        "excerpt": "SAP runs our production planning.",
    }
    with pytest.raises(ModelOutputInvalid, match="Correct the discovery quote"):
        research(Recorder(company), catalogs, "skill", candidate, {}, excerpts=[page])
    claim = company.evidence[0].model_copy(
        update={
            "id": "software-use",
            "text": "The firm uses SAP.",
            "source_url": page["url"],
            "quote": page["excerpt"],
        }
    )
    company.evidence.append(claim)
    company.software[0].evidence_ids = [claim.id]
    dossier, _ = research(Recorder(company), catalogs, "skill", candidate, {}, excerpts=[page])
    assert next(c for c in dossier.evidence if c.id == claim.id).quote == page["excerpt"]


def test_report_exports_cross_run_coverage_and_uncovered_apps(root, company, workflow, monkeypatch):
    directory, state, _ = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    other = company.model_dump()
    other.update(id="other", real_firm="Other firm")
    other["software"][0]["catalog_app_ids"] = ["SAP_mock"]
    write(root / "data" / "companies" / "other" / "accepted.json", other)
    report = export(root, directory, state)
    assert report["coverage"]["app_coverage"]["SAP_mock"]["accepted_firms"] == 1
    assert "SAP_mock" not in report["coverage"]["uncovered_apps"]
    assert "12306_mock" in report["coverage"]["uncovered_apps"]
    saved = read(directory / "dataset.json")["coverage"]
    assert saved["app_coverage"] == report["coverage"]["app_coverage"]
    assert saved["uncovered_apps"] == report["coverage"]["uncovered_apps"]
    markdown = (directory / "REPORT.md").read_text()
    assert "Uncovered apps:" in markdown and "| SAP_mock | 1 | 0 |" in markdown
