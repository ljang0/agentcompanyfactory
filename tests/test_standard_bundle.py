"""Standard workplace infrastructure survives research and task-independent export."""

import tomllib

import pytest
from test_research_references import Recorder

from company_envs.catalogs import Catalogs
from company_envs.company_layout import export_company, export_run
from company_envs.models import ModelOutputInvalid
from company_envs.research import research
from company_envs.schemas import Candidate, Software
from company_envs.storage import digest, read, write

STANDARD_APPS = {
    "gmail_mock": ("Gmail", "Email customers about instrument requirements"),
    "slack_mock": ("Slack", "Chat with engineers about design changes"),
    "google_calendar_mock": ("Google Calendar", "Schedule recurring engineering reviews"),
    "google_drive_mock": ("Google Drive", "Store shared specifications and old versions"),
    "google_docs_mock": ("Google Docs", "Collaborate on engineering documents"),
}


@pytest.fixture
def bundle_company(company):
    company.software.extend(
        Software(
            id=app_id,
            capability=capability,
            reference_product=product,
            catalog_app_ids=[app_id],
            status="substitution",
            rationale="Synthetic workplace infrastructure, not a claim of real-firm adoption.",
            evidence_ids=[],
        )
        for app_id, (product, capability) in STANDARD_APPS.items()
    )
    return company


def research_bundle(root, models, **kwargs):
    company = models.company
    candidate = Candidate(
        id=company.id,
        real_firm=company.real_firm,
        website=company.website,
        sector=company.sector,
        reason="Relevant operation",
    )
    return research(models, Catalogs(root / "catalogs"), "skill", candidate, {}, **kwargs)


@pytest.fixture
def models(bundle_company):
    recorder = Recorder(bundle_company, False)
    recorder.config["design"].update(
        standard_apps=list(STANDARD_APPS), available_runtime_apps=list(STANDARD_APPS)
    )
    return recorder


@pytest.mark.parametrize("missing", STANDARD_APPS)
@pytest.mark.parametrize("repair", [False, True])
def test_missing_standard_app_rejects_dossier_with_repair_receipt(root, models, missing, repair):
    models.company.software = [s for s in models.company.software if s.catalog_app_ids != [missing]]
    with pytest.raises(ModelOutputInvalid, match=f"missing mandatory standard apps: {missing}") as error:
        research_bundle(root, models, previous=models.company if repair else None)
    assert error.value.raw == models.company.model_dump()
    assert error.value.receipt == {"job": "research_repair" if repair else "research"}


@pytest.mark.parametrize("surface_declared", [False, True])
def test_research_passes_mandatory_bundle_and_preserves_surface_enum(root, models, surface_declared):
    if not surface_declared:
        models.config["design"].pop("available_runtime_apps")
    result, _ = research_bundle(root, models)
    assert result is models.company
    _, payload, kwargs = models.calls[0]
    assert payload["standard_apps"] == list(STANDARD_APPS)
    enum = kwargs["schema"]["$defs"]["Software"]["properties"]["catalog_app_ids"]["items"]["enum"]
    if surface_declared:
        assert enum == sorted(STANDARD_APPS)
    else:
        assert set(STANDARD_APPS) <= set(enum)


@pytest.mark.parametrize("surface", [[], ["gmail_mock"]])
def test_standard_app_outside_surface_fails_before_model_call(root, models, surface):
    models.config["design"]["available_runtime_apps"] = surface
    with pytest.raises(ValueError, match=r"design.standard_apps.*design.available_runtime_apps.*slack_mock"):
        research_bundle(root, models)
    assert models.calls == []


@pytest.fixture
def selected_run(root, bundle_company, workflow):
    bundle_company.software[0].catalog_app_ids = ["jira_mock"]
    bundle_company.software[0].status = "substitution"
    workflow.software_requirement_ids.append("slack_mock")
    company_path = "data/company.json"
    workflow_path = "data/workflow.json"
    review_path = "data/review.json"
    write(root / company_path, bundle_company.model_dump())
    write(root / workflow_path, workflow.model_dump())
    write(root / review_path, {"verdict": "accept"})
    write(
        root / "runs" / "bundle" / "dataset.json",
        {
            "companies": {bundle_company.id: company_path},
            "workflows": [
                {
                    "company_id": bundle_company.id,
                    "workflow_id": workflow.id,
                    "workflow_path": workflow_path,
                    "review_path": review_path,
                }
            ],
        },
    )
    return "bundle", bundle_company.id


@pytest.mark.parametrize("whole_run", [False, True])
def test_export_includes_unused_standard_apps_and_deduplicates_task_overlap(root, selected_run, whole_run):
    run_id, company_id = selected_run
    config = tomllib.loads((root / "config.toml").read_text())
    assert config["design"]["standard_apps"] == list(STANDARD_APPS)
    if whole_run:
        folder = export_run(root, run_id)[0]
    else:
        folder, _ = export_company(root, run_id, company_id)
    exported = read(folder / "apps.json")
    apps = {app["app_id"]: app for app in exported["apps"]}
    assert len(exported["apps"]) == len(STANDARD_APPS) + 1
    assert {app_id: app["role"] for app_id, app in apps.items()} == {
        **dict.fromkeys(STANDARD_APPS, "standard"),
        "jira_mock": "task",
    }
    for app_id, app in apps.items():
        assert app["hub_seedable"] is True
        assert (root / app["schema"]).is_file()
        assert app["top_level_keys"]
        assert app["state_file"] == f"world/{app_id}.state.json"
    assert read(folder / "MANIFEST.json")["hashes"]["apps.json"] == digest(
        (folder / "apps.json").read_bytes()
    )


@pytest.mark.parametrize("whole_run", [False, True])
def test_explicit_export_config_overrides_root_bundle(root, selected_run, whole_run):
    run_id, company_id = selected_run
    config = {"design": {"standard_apps": ["gmail_mock"]}}
    if whole_run:
        folder = export_run(root, run_id, config=config)[0]
    else:
        folder, _ = export_company(root, run_id, company_id, config=config)
    assert {app["app_id"]: app["role"] for app in read(folder / "apps.json")["apps"]} == {
        "gmail_mock": "standard",
        "slack_mock": "task",
        "jira_mock": "task",
    }


@pytest.mark.parametrize("whole_run", [False, True])
def test_taskless_export_keeps_standard_bundle_and_domain_candidates(root, selected_run, whole_run):
    run_id, company_id = selected_run
    dataset_path = root / "runs" / run_id / "dataset.json"
    dataset = read(dataset_path)
    dataset["workflows"] = []
    write(dataset_path, dataset)
    with pytest.raises(ValueError, match="no selected workflows"):
        export_company(root, run_id, company_id)
    folder = (
        export_run(root, run_id, tasks_required=False)[0]
        if whole_run
        else export_company(root, run_id, company_id, tasks_required=False)[0]
    )
    assert list((folder / "tasks").iterdir()) == []
    assert read(folder / "MANIFEST.json")["tasks"] == []
    assert {app["app_id"]: app["role"] for app in read(folder / "apps.json")["apps"]} == {
        **dict.fromkeys(STANDARD_APPS, "standard"),
        "jira_mock": "task",
    }
