"""Run admission validation: no providers and no artifacts for invalid design."""

import copy
import tomllib

import pytest

from company_envs import pipeline
from company_envs.storage import read


@pytest.fixture
def config(root, monkeypatch):
    value = tomllib.loads((root / "config.toml").read_text())
    # Exercise new_run admission with explicit values, including malformed
    # structures, without changing the repository's production configuration.
    monkeypatch.setattr(pipeline, "load_config", lambda text: copy.deepcopy(value))
    return value


@pytest.mark.parametrize(
    "design,match",
    [
        ("digital", "design must be a table"),
        ({"execution_mode": "unknown"}, "execution_mode"),
        ({"execution_mode": "Digital"}, "execution_mode"),
        ({"execution_mode": True}, "execution_mode"),
        ({"execution_mode": []}, "execution_mode"),
        ({"workflow_version": 2}, "workflow_version"),
        ({"execution_mode": "digital"}, "digital execution_mode"),
        (
            {"execution_mode": "digital", "workflow_version": "1", "minimum_workers": 3},
            "digital execution_mode",
        ),
        ({"execution_mode": "digital", "workflow_version": "2"}, "digital execution_mode"),
        (
            {"execution_mode": "digital", "workflow_version": "2", "minimum_workers": 2},
            "digital execution_mode",
        ),
        ({"execution_mode": "digital", "workflow_version": "2", "minimum_workers": True}, "minimum_workers"),
        ({"execution_mode": "digital", "workflow_version": "2", "minimum_workers": 3.0}, "minimum_workers"),
        *[
            ({"available_runtime_apps": apps}, "available_runtime_apps")
            for apps in (
                None,
                [],
                "google_docs_mock",
                {},
                [""],
                [None],
                [3],
                [["google_docs_mock"]],
                ["google_docs_mock", "google_docs_mock"],
                ["unknown_app"],
                ["zendesk_mock"],
                ["google_docs_mock", "unknown_app"],
            )
        ],
    ],
)
def test_invalid_design_is_rejected_before_any_run_artifact_or_model(
    root, config, monkeypatch, design, match
):
    config["design"] = design
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid design reached run preparation or a model")

    monkeypatch.setattr(pipeline, "freeze_skills", forbidden)
    monkeypatch.setattr(pipeline, "Models", forbidden)
    with pytest.raises(ValueError, match=match):
        pipeline.new_run(root, 1, 1)
    assert not (root / "runs").exists()
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize(
    "design",
    [
        None,
        {},
        {"execution_mode": "scheduled"},
        {"execution_mode": "scheduled", "workflow_version": "2", "minimum_workers": 2},
        {"execution_mode": "digital", "workflow_version": "2", "minimum_workers": 3},
        {"execution_mode": "digital", "workflow_version": "2", "minimum_workers": 7},
        {"available_runtime_apps": ["google_docs_mock", "Zendesk_mock"]},
    ],
)
def test_valid_design_preserves_explicit_configuration_and_legacy_absence(root, config, monkeypatch, design):
    if design is None:
        config.pop("design", None)
    else:
        config["design"] = design

    def forbidden(*args, **kwargs):
        raise AssertionError("Creating a run must not call a model")

    monkeypatch.setattr(pipeline, "Models", forbidden)
    directory = pipeline.new_run(root, 1, 1)
    retained = read(directory / "run.json")
    assert retained["status"] == "ready"
    assert retained["config"]["design"] == {**(design or {}), "feature_matrix": True}
    assert retained["jobs"] == {}


def test_available_runtime_apps_uses_actual_catalog_not_a_hardcoded_adapter_allowlist(root, config):
    app_ids = [row["id"] for row in read(root / "catalogs/apps.json")["apps"]]
    config["design"] = {"available_runtime_apps": app_ids}
    directory = pipeline.new_run(root, 1, 1)
    assert read(directory / "run.json")["config"]["design"]["available_runtime_apps"] == app_ids
