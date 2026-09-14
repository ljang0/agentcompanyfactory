from types import SimpleNamespace

import numpy as np
import pytest

from company_envs import pipeline
from company_envs.catalogs import Catalogs
from company_envs.diversity import nearest
from company_envs.holdout import overlap
from company_envs.pipeline import validate_run_inputs
from company_envs.portfolio import freeze_history, load_history, summarize
from company_envs.schemas import Candidate, Discovery
from company_envs.storage import digest, read, write


def historical_run(root, name, workflow):
    path = root / "runs" / name
    artifact = f"data/workflows/{workflow.id}/{digest(workflow.model_dump())}.json"
    write(root / artifact, workflow.model_dump())
    entry = {
        "workflow_id": workflow.id,
        "verdict": "accept",
        "workflow_path": artifact,
        "family_id": workflow.id,
        "review_path": f"runs/{name}/review.json",
        "review_input_hash": "bound-input",
    }
    write(path / "run.json", {"id": name, "created_at": name, "entries": [entry]})
    return path


def test_freezes_verified_latest_version_and_records_quarantine(root, workflow, monkeypatch):
    historical_run(root, "01", workflow)
    changed = workflow.model_copy(update={"decision_problem": "New accepted decision"})
    historical_run(root, "02", changed)
    calls = []

    def verify(root, directory, state):
        calls.append(directory.name)
        return state["entries"], {"bad": "stale review"}

    monkeypatch.setattr("company_envs.report.verified_entries", verify)
    directory = root / "runs" / "new"
    manifest = freeze_history(root, directory)
    assert calls == ["01", "02"]
    assert manifest["count"] == 1
    assert manifest["quarantined"] == 2
    state = {"portfolio": manifest}
    frozen = load_history(directory, state)
    assert frozen[0].decision_problem == changed.decision_problem
    assert frozen[0].id != workflow.id
    assert read(directory / "portfolio.json")["workflows"][0]["origin"]["run"] == "runs/02"
    # Subsequent live history, including a broken original artifact, is not consulted.
    write(root / "runs" / "02" / "run.json", {"changed": True})
    historical_run(root, "03", workflow)
    assert load_history(directory, state) == frozen
    snapshot = read(directory / "portfolio.json")
    snapshot["workflows"][0]["workflow"]["decision_problem"] = "Tampered"
    write(directory / "portfolio.json", snapshot)
    with pytest.raises(ValueError, match="frozen portfolio changed"):
        load_history(directory, state)


def test_unverified_acceptance_is_not_history(root, workflow):
    historical_run(root, "unverified", workflow)
    directory = root / "runs" / "new"
    manifest = freeze_history(root, directory)
    assert manifest["count"] == 0
    assert manifest["quarantined"] == 1
    assert load_history(directory, {"portfolio": manifest}) == []


def test_legacy_run_does_not_read_live_history(root, workflow):
    historical_run(root, "old", workflow)
    assert load_history(root / "runs" / "legacy", {}) == []


def test_history_same_original_id_remains_comparable(root, workflow, monkeypatch):
    historical_run(root, "old", workflow)
    monkeypatch.setattr("company_envs.report.verified_entries", lambda r, d, s: (s["entries"], {}))
    directory = root / "runs" / "new"
    manifest = freeze_history(root, directory)
    history = load_history(directory, {"portfolio": manifest})
    encoder = SimpleNamespace(encode=lambda text: np.array([1.0, 0.0]))
    results = nearest(workflow, history + [workflow], encoder, k=1)
    assert len(results) == 1
    assert results[0]["workflow_id"] == history[0].id


def test_summary_bounded_deterministic_substantive_and_company_balanced(workflow):
    workflows = [
        workflow.model_copy(update={"id": f"task_{i}", "company_id": "a" if i < 5 else "b"}) for i in range(6)
    ]
    result = summarize(workflows, limit=2)
    assert result == summarize(list(reversed(workflows)), limit=2)
    assert result["total_workflows"] == 6
    assert result["shown_workflows"] == 2
    assert {row["company_id"] for row in result["work"]} == {"a", "b"}
    row = result["work"][0]
    assert row["decision_problem"] == workflow.decision_problem
    assert row["contributions"] == [c.work for c in workflow.contributions]
    assert row["decisions_and_effects"][0]["effect"] == workflow.phases[0].downstream_effect
    assert row["constraints_or_initial_context"] == workflow.initial_materials


def test_legal_suffix_aliases_but_not_business_identity_or_occupations(company):
    original = {**company.model_dump(), "real_firm": "Example & Sons, Inc."}
    alias = {
        **original,
        "id": "different",
        "website": "https://other.test",
        "real_firm": "Example and Sons Incorporated",
    }
    assert overlap(alias, [original])
    unrelated = {**alias, "real_firm": "Example and Sons Healthcare"}
    assert not overlap(unrelated, [original])


def test_reference_provenance_checked_for_ordinary_run(root):
    directory = root / "runs" / "new"
    value = {"origin": "upstream", "version": "pinned"}
    write(directory / "references.json", value)
    state = {"reference_provenance": {"path": "references.json", "hash": digest(value)}}
    validate_run_inputs(directory, state)
    write(directory / "references.json", {"changed": True})
    with pytest.raises(ValueError, match="reference provenance changed"):
        validate_run_inputs(directory, state)


def test_holdout_matches_name_id_or_host_not_role(company):
    original = company.model_dump()
    assert overlap({**original, "real_firm": "Alias", "website": "https://new.test"}, [original])
    assert overlap(
        {**original, "id": "new", "real_firm": "EXAMPLE REAL-FIRM", "website": "https://new.test"}, [original]
    )
    assert overlap(
        {**original, "id": "new", "real_firm": "Alias", "website": "https://www.example.com/jobs"}, [original]
    )
    assert not overlap(
        {**original, "id": "new", "real_firm": "New firm", "website": "https://different.test"}, [original]
    )


def test_discovery_exclusion_is_enforced_and_recorded(root, company, monkeypatch):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    state["excluded_companies"] = [company.model_dump()]
    monkeypatch.setattr(pipeline, "coverage", lambda *args: {"companies": 0, "tasks": 0, "sectors": {}})

    def discover(models, catalogs, prompt, sector, existing, cov, count):
        assert company.real_firm in existing
        return Discovery(
            candidates=[
                Candidate(
                    id="renamed",
                    real_firm="A new alias",
                    website="https://www.example.com/about",
                    sector=sector,
                    reason="Would reuse a historical firm",
                )
            ]
        ), {"call_id": "test"}

    monkeypatch.setattr(pipeline.research_job, "discover", discover)
    assert not pipeline.fill_queue(root, directory, state, Catalogs(directory / "catalogs"))
    assert not state["jobs"]
    assert state["excluded_discoveries"][0]["matches"] == [company.real_firm]


@pytest.mark.parametrize("marker", ["state", "file"])
def test_archived_campaign_cannot_resume_or_publish_as_ordinary_run(root, monkeypatch, marker):
    from company_envs.report import verified_entries

    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    if marker == "state":
        state["campaign_hash"] = "historical-campaign"
    else:
        write(directory / "campaign.json", {"historical": True})
    write(directory / "run.json", state)
    before = (directory / "run.json").read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("archived campaign must be refused before loading model machinery")

    monkeypatch.setattr(pipeline, "Embeddings", forbidden)
    with pytest.raises(ValueError, match="campaigns are archived"):
        pipeline.run(root, directory)
    assert (directory / "run.json").read_bytes() == before
    entries, errors = verified_entries(root, directory, state)
    assert not entries
    assert "campaigns are archived" in errors["inputs"]


@pytest.mark.parametrize("manifest", ["portfolio", "reference_provenance"])
def test_frozen_input_paths_cannot_escape_run(root, manifest):
    directory = root / "runs" / "new"
    outside = root / "outside.json"
    write(outside, {})
    state = {manifest: {"path": "../../outside.json", "hash": digest({}), "count": 0}}
    with pytest.raises(ValueError, match="path escapes run directory"):
        validate_run_inputs(directory, state)


@pytest.mark.parametrize("flag", [None, False])
def test_legacy_coverage_has_no_feature_matrix_fields(root, workflow, flag):
    directory = pipeline.new_run(root, 1, 1)
    state = read(directory / "run.json")
    design = state["config"]["design"]
    design.pop("feature_matrix", None)
    if flag is not None:
        design["feature_matrix"] = flag
    snapshot = {
        "workflows": [{"workflow": workflow.model_dump(), "content_hash": digest(workflow.model_dump())}]
    }
    write(directory / "portfolio.json", snapshot)
    state["portfolio"] = {"path": "portfolio.json", "hash": digest(snapshot), "count": 1}
    catalogs = Catalogs(directory / "catalogs")
    result = pipeline.coverage(root, state, catalogs)
    assert result["portfolio"]["total_workflows"] == 1
    assert "cell_histogram" not in result["portfolio"]
    assert "cell_histogram" not in result and "company_feature_cells" not in result
    del state["portfolio"]
    assert pipeline.coverage(root, state, catalogs) == {
        key: value for key, value in result.items() if key != "portfolio"
    }
