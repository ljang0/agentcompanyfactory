"""Portable configuration and frozen research contracts; no providers or app runtime."""

import json
import os
import shutil
import sys

import pytest

from company_envs import __main__ as cli
from company_envs import pipeline
from company_envs.company_layout import export_company, verify_pins
from company_envs.config import CONFIG_ENV, load_config, selected_config
from company_envs.doctor import inspect_prerequisites
from company_envs.research_bundle import amend_dossier, research_inputs
from company_envs.sources import Sources
from company_envs.storage import digest, read, write


@pytest.fixture
def research_run(tmp_path, company, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    company.workers.append(company.workers[0].model_copy(update={"id": "boss"}))
    company.teams[0].worker_ids.append("boss")
    company.outlines[0].worker_ids.append("boss")
    company.software[0].catalog_app_ids = ["google_docs_mock"]
    (root / "config.toml").write_text(
        '[design]\nminimum_workers = 3\nmodel_pin = "codex/gpt-6-astra"\n'
        f'hub_revision = "{"a" * 40}"\nhub_root = "hub"\n'
        'available_runtime_apps = ["google_docs_mock"]\n'
    )
    schema = root / "catalogs/app_schemas/google_docs_mock.md"
    schema.parent.mkdir(parents=True)
    schema.write_text("## State Schema\n\n| Key | Type |\n| --- | --- |\n| `documents` | array |\n")
    write(
        root / "catalogs/apps.json",
        {
            "apps": [
                {
                    "id": "google_docs_mock",
                    "source": "cua_gym_hub",
                    "schema": str(schema),
                    "state_keys": ["documents"],
                }
            ]
        },
    )
    write(
        root / "catalogs/manifest.json",
        {
            "files": {
                "app_schemas/google_docs_mock.md": {"sha256": digest(schema.read_bytes())},
                "apps.json": {"sha256": digest((root / "catalogs/apps.json").read_bytes())},
            }
        },
    )
    write(root / "data/companies/example/version.json", company.model_dump())
    write(
        root / "runs/run/dataset.json",
        {
            "companies": {"example": "data/companies/example/version.json"},
            "workflows": [],
        },
    )
    write(root / "runs/run/jobs/example/company.json", company.model_dump())
    claim = company.evidence[0]
    page = {
        "url": claim.source_url,
        "text": claim.quote,
        "text_hash": digest(claim.quote),
        "status": "captured",
        "captured_at": "2026-09-01",
    }
    write(root / "data/sources" / f"{digest(claim.source_url)}.json", page)
    write(
        root / "runs/run/jobs/example/evidence.json",
        [
            {
                "claim_id": claim.id,
                "kind": claim.kind,
                "status": "supported",
                "source_url": claim.source_url,
                "quote": claim.quote,
                "text_hash": page["text_hash"],
            }
        ],
    )
    return root, company


def test_dossier_only_drops_old_tasks_but_retains_research(research_run):
    root, company = research_run
    dataset = read(root / "runs/run/dataset.json")
    # Old workflow paths deliberately do not exist: a fresh dossier must not read them.
    dataset["workflows"] = [{"company_id": "example", "workflow_path": "old/private-solution.json"}]
    write(root / "runs/run/dataset.json", dataset)
    folder, manifest = export_company(root, "run", "example", dossier_only=True)
    assert manifest["tasks"] == []
    assert manifest["source_paths"]["workflows"] == []
    assert list((folder / "tasks").iterdir()) == []
    assert not (folder / "review.json").exists()
    assert read(folder / "company.json") == company.model_dump()
    assert manifest["research"]["status"] == "verified"
    assert manifest["source_paths"]["evidence"] == "runs/run/jobs/example/evidence.json"
    assert all(digest((folder / p).read_bytes()) == h for p, h in manifest["hashes"].items())


def test_exported_research_survives_cache_and_run_removal(research_run, monkeypatch):
    root, company = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    shutil.rmtree(root / "runs")
    shutil.rmtree(root / "data")
    monkeypatch.setattr(Sources, "capture", lambda *a: pytest.fail("must not retrieve sources"))
    evidence, excerpts, provenance = research_inputs(root, folder, company)
    assert evidence[0]["status"] == "supported"
    assert excerpts[0]["excerpt"] == company.evidence[0].quote
    assert provenance["captured_pages"] == 1


@pytest.mark.parametrize("change", ["dossier", "source", "evidence", "missing_source"])
def test_damaged_bundle_never_falls_back_to_good_mutable_cache(research_run, change):
    root, company = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    source = next((folder / "research/sources").glob("*.json"))
    if change == "dossier":
        company.operations += " Changed."
    elif change == "missing_source":
        source.unlink()
    else:
        (source if change == "source" else folder / "research/evidence.json").write_text("{}")
    with pytest.raises(ValueError, match="research bundle"):
        research_inputs(root, folder, company)


@pytest.mark.parametrize("change", ["claim", "source_text", "source_hash", "missing_job"])
def test_unbound_or_changed_source_remains_unverified(research_run, change):
    root, company = research_run
    if change == "claim":
        job = root / "runs/run/jobs/example/company.json"
        data = read(job)
        data["evidence"][0]["text"] = "Different evidence claim"
        write(job, data)
    elif change == "missing_job":
        (root / "runs/run/jobs/example/evidence.json").unlink()
    else:
        source = next((root / "data/sources").glob("*.json"))
        page = read(source)
        page["text"] += " Replaced source."
        if change == "source_hash":
            page["text_hash"] = digest(page["text"])
        write(source, page)
    folder, manifest = export_company(root, "run", "example", dossier_only=True)
    evidence, excerpts, provenance = research_inputs(root, folder, company)
    assert manifest["research"]["status"] == "unverified"
    assert evidence[0]["status"] == "unverified"
    assert excerpts == []
    assert provenance["issues"]


def test_legacy_folder_finds_matching_job_evidence(research_run):
    root, company = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    shutil.rmtree(folder / "research")
    evidence, excerpts, _ = research_inputs(root, folder, company)
    assert evidence[0]["status"] == "supported"
    assert excerpts


@pytest.mark.parametrize(
    "defect",
    [
        "missing_claim",
        "duplicate_claim",
        "missing_evidence_hash",
        "missing_source_hash",
        "wrong_url",
        "wrong_kind",
    ],
)
def test_research_rechecks_complete_claim_binding_even_with_updated_file_hashes(research_run, defect):
    root, company = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    research = folder / "research"
    rows, proof = read(research / "evidence.json"), read(research / "PROVENANCE.json")
    if defect == "missing_claim":
        rows.pop()
    elif defect == "duplicate_claim":
        rows.append(dict(rows[0]))
    elif defect == "missing_evidence_hash":
        proof["hashes"].pop("evidence.json")
    elif defect == "missing_source_hash":
        proof["hashes"] = {k: v for k, v in proof["hashes"].items() if not k.startswith("sources/")}
    elif defect == "wrong_url":
        rows[0]["source_url"] = "https://different-source.invalid"
    else:
        rows[0]["kind"] = "synthetic"
    write(research / "evidence.json", rows)
    if "evidence.json" in proof["hashes"]:
        proof["hashes"]["evidence.json"] = digest((research / "evidence.json").read_bytes())
    write(research / "PROVENANCE.json", proof)
    with pytest.raises(ValueError, match="research bundle"):
        research_inputs(root, folder, company)


@pytest.mark.parametrize(
    "defect", ["workers", "keys", "identity", "duplicate_app", "missing_app", "missing_catalog_hash"]
)
def test_doctor_compares_export_contract_to_dossier_and_catalog(research_run, defect):
    root, _ = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    apps = read(folder / "apps.json")
    if defect == "workers":
        apps["workers"] = ["unknown-worker"]
    elif defect == "keys":
        apps["apps"][0]["top_level_keys"] = ["invented"]
    elif defect == "identity":
        apps["identity"] = "shared_user"
    elif defect == "duplicate_app":
        apps["apps"].append(dict(apps["apps"][0]))
    elif defect == "missing_app":
        apps["apps"] = []
    else:
        manifest = read(root / "catalogs/manifest.json")
        manifest["files"].pop("apps.json")
        write(root / "catalogs/manifest.json", manifest)
    write(folder / "apps.json", apps)
    report = inspect_prerequisites(root, folder=folder)
    contract = next(
        r
        for r in report["checks"]
        if r["check"] == ("catalog_coverage" if defect == "missing_catalog_hash" else "export_app_contract")
    )
    assert contract["status"] == "fail"


def test_construction_amendment_preserves_bound_research_and_original(research_run):
    root, company = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    replacement = company.model_dump()
    replacement["assumptions"] = ["Workers own their personal notes."]
    amend_dossier(folder, replacement, "Reconcile ordinary role boundaries")
    assert read(folder / "research/dossier-original.json") == company.model_dump()
    assert research_inputs(root, folder, replacement)[0][0]["status"] == "supported"
    replacement["evidence"][0]["text"] = "Changed evidence"
    with pytest.raises(ValueError, match="research claims"):
        amend_dossier(folder, replacement, "Invalid edit")


@pytest.mark.parametrize("child_matches", [True, False])
def test_pin_verification_distinguishes_parent_from_pinned_hub_submodule(
    research_run, monkeypatch, child_matches
):
    root, _ = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    parent = root / "cua"
    child = parent / "hub"

    def git(args, **kwargs):
        command = args[3:]
        if command == ["rev-parse", "HEAD"]:
            return ("a" if args[2] == str(parent) else "b") * 40 + "\n"
        if command == ["rev-parse", "--show-superproject-working-tree"]:
            return str(parent)
        if command == ["rev-parse", "--show-toplevel"]:
            return str(child)
        assert command[:2] == ["ls-tree", "a" * 40]
        return f"160000 commit {('b' if child_matches else 'c') * 40}\thub\n"

    monkeypatch.setattr("company_envs.company_layout.subprocess.check_output", git)
    report = verify_pins(root, folder)
    assert report["ok"] is child_matches
    assert report["superproject_revision"] == "a" * 40


def test_doctor_is_read_only_and_marks_runtime_unmeasured(research_run):
    root, _ = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = inspect_prerequisites(root, folder=folder)
    assert report["ok"], report
    assert (
        next(r for r in report["checks"] if r["check"] == "app_surface")["detail"]["runtime_behavior"]
        == "unmeasured"
    )
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    (root / "catalogs/app_schemas/google_docs_mock.md").write_text("damaged")
    assert not inspect_prerequisites(root, folder=folder)["ok"]


def test_config_precedence_paths_and_restoration(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config.toml").write_text('[design]\nhub_root = "root-hub"\n')
    settings = tmp_path / "settings"
    settings.mkdir()
    override = settings / "pilot.toml"
    override.write_text('[design]\nhub_root = "../hub"\n[models]\ncodex_homes = ["accounts/a"]\n')
    assert load_config(root)["design"]["hub_root"] == str(root / "root-hub")
    monkeypatch.setenv(CONFIG_ENV, str(root / "config.toml"))
    with selected_config(override):
        assert os.environ[CONFIG_ENV] == str(override)
        config = load_config(root)
        assert config["design"]["hub_root"] == str(tmp_path / "hub")
        assert config["models"]["codex_homes"] == [str(settings / "accounts/a")]
        assert load_config(root, root / "config.toml")["design"]["hub_root"] == str(root / "root-hub")
    assert os.environ[CONFIG_ENV] == str(root / "config.toml")
    with pytest.raises(RuntimeError), selected_config(override):
        raise RuntimeError("failed command")
    assert os.environ[CONFIG_ENV] == str(root / "config.toml")


def test_missing_explicit_config_cannot_use_legacy_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    assert load_config(tmp_path, missing_ok=True) == {}
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "typo.toml"))
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path, missing_ok=True)


def test_new_run_freezes_selected_config(root, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    selected = root / "pilot.toml"
    selected.write_text((root / "config.toml").read_text().replace("companies = 50", "companies = 1"))
    with selected_config(selected):
        folder = pipeline.new_run(root, task_target=2)
    # A later edit of the selectable file does not rewrite a recorded run.
    selected.write_text("invalid config")
    saved = read(folder / "run.json")["config"]
    assert saved["generation"]["companies"] == 1
    assert saved["design"]["minimum_workers"] == 3


def test_doctor_reports_malformed_tables(tmp_path, monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    (tmp_path / "config.toml").write_text('design = "invalid"\n')
    report = inspect_prerequisites(tmp_path)
    assert not report["ok"]
    assert report["checks"][-1]["check"] == "configuration_tables"


def test_doctor_without_folder_uses_submodule_aware_verification(research_run, monkeypatch):
    root, _ = research_run
    calls = []

    def verify(root, folder):
        calls.append((root, folder))
        return {"ok": True, "superproject_revision": "a" * 40, "pinned_submodule_revision": "b" * 40}

    monkeypatch.setattr("company_envs.doctor.verify_pins", verify)
    report = inspect_prerequisites(root, profile="desktop")
    assert calls == [(root, None)]
    assert next(r for r in report["checks"] if r["check"] == "hub_checkout")["status"] == "pass"


def test_desktop_prerequisite_failures_do_not_claim_execution(research_run, monkeypatch):
    root, _ = research_run
    folder, _ = export_company(root, "run", "example", dossier_only=True)
    monkeypatch.setattr("company_envs.doctor.shutil.which", lambda _: None)
    monkeypatch.setattr("company_envs.doctor.verify_pins", lambda *a: {"ok": False, "drift": ["wrong HEAD"]})
    result = inspect_prerequisites(root, folder=folder, profile="desktop")
    rows = {row["check"]: row for row in result["checks"]}
    assert not result["ok"]
    assert rows["hub_checkout"]["status"] == "fail"
    assert rows["vm_base_image"]["status"] == "fail"
    assert rows["executable:node"]["status"] == "fail"
    assert rows["desktop_execution"]["status"] == "unmeasured"


def test_cli_doctor_uses_override_and_returns_failure(research_run, monkeypatch, capsys):
    root, _ = research_run
    override = root / "pilot.toml"
    override.write_text((root / "config.toml").read_text())
    (root / "config.toml").write_text("bad default config")
    monkeypatch.setattr(
        sys, "argv", ["company-envs", "--root", str(root), "--config", str(override), "doctor"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["ok"]
    assert CONFIG_ENV not in os.environ
    override.write_text("invalid config")
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
