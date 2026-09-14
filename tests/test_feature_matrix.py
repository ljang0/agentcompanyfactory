"""Deterministic planning and structural validation; no model, VM or npm calls."""

import copy
import json
import tomllib
from collections import Counter
from types import SimpleNamespace

import pytest
from test_pipeline import Critic, setup_job

from company_envs import pipeline, workflows
from company_envs.catalogs import Catalogs
from company_envs.models import ModelOutputInvalid
from company_envs.portfolio import cell_histogram, cell_key, freeze_history, load_history, summarize
from company_envs.report import build_report, corpus_difficulty, export, label_difficulty
from company_envs.schemas import Workflow as SharedWorkflow
from company_envs.storage import digest, read, run_lock, write
from company_envs.world.hub_app import record_collections


@pytest.fixture
def two_app_company(company):
    company.software[0].catalog_app_ids = ["google_docs_mock"]
    company.software[0].status = "catalog_candidate"
    company.software.append(
        company.software[0].model_copy(update={"id": "sheets", "catalog_app_ids": ["google_sheets_mock"]})
    )
    company.outlines.append(company.outlines[0].model_copy(update={"id": "second"}))
    return company


class Author:
    def __init__(self, workflow, mutate=None, enabled=True):
        self.workflow = workflow
        self.mutate = mutate
        self.config = {"design": {"feature_matrix": enabled}, "generation": {"seed": 42}}
        self.receipt = {"model": "codex/fake-author", "job": "expand", "call_id": "matrix-test"}

    def call(self, stage, prompt, response_type, **options):
        assert stage == "expand"
        self.payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.schema = options["schema"]
        rows = self.payload.get("feature_matrix", [{"outline_id": "release"}])
        output = []
        for row in rows:
            oid = row.get("outline_id", self.payload["eligible_outline_ids"][len(output)])
            raw = {**self.workflow.model_dump(), "id": f"example_{oid}", "outline_id": oid}
            if "feature_cell" in row:
                raw["feature_cell"] = copy.deepcopy(row["feature_cell"])
                raw["difficulty"] = row["difficulty"]
            output.append(raw)
        if self.mutate:
            self.mutate(output)
        return response_type.model_validate(
            {"workflows": output, "amendment": None, "selection_reason": "Cover assigned cells"}
        ), self.receipt


def design(root, company, author, prompt="skill", **options):
    return workflows.design(
        author,
        prompt,
        company,
        [],
        Catalogs(root / "catalogs"),
        [],
        2,
        read_only_tools=False,
        allow_amendment=False,
        **options,
    )


def test_matrix_uses_two_pinned_app_schemas_and_seed(root, two_app_company):
    catalogs = Catalogs(root / "catalogs")
    expected = {
        f"{app_id}.{key}"
        for app_id in ("google_docs_mock", "google_sheets_mock")
        for key in record_collections((catalogs.path / "app_schemas" / f"{app_id}.md").read_text())
    }
    assert set(workflows.company_collections(two_app_company, catalogs)) == expected
    matrix = workflows.build_feature_matrix(two_app_company, catalogs, 8, 42)
    assert matrix == workflows.build_feature_matrix(two_app_company, catalogs, 8, 42)
    assert matrix != workflows.build_feature_matrix(two_app_company, catalogs, 8, 43)
    two_app_company.software.reverse()
    assert matrix == workflows.build_feature_matrix(two_app_company, catalogs, 8, 42)
    assert len({cell_key(row["feature_cell"]) for row in matrix}) == 8
    assert {c.split(".")[0] for row in matrix for c in row["feature_cell"]["collections"]} == {
        "google_docs_mock",
        "google_sheets_mock",
    }
    assert all(set(row["feature_cell"]["collections"]) <= expected for row in matrix)


def test_unused_cells_precede_used_cells_and_same_company_cells_are_reserved(root, two_app_company):
    catalogs = Catalogs(root / "catalogs")
    matrix = workflows.build_feature_matrix(two_app_company, catalogs, 2, 42)
    used = {cell_key(row["feature_cell"]): 4 for row in matrix}
    next_matrix = workflows.build_feature_matrix(two_app_company, catalogs, 2, 42, {"cell_histogram": used})
    assert not used.keys() & {cell_key(row["feature_cell"]) for row in next_matrix}
    reserved = {"company_feature_cells": {two_app_company.id: [row["feature_cell"] for row in matrix]}}
    assert not used.keys() & {
        cell_key(row["feature_cell"])
        for row in workflows.build_feature_matrix(two_app_company, catalogs, 2, 42, reserved)
    }


def test_exhausted_portfolio_uses_least_used_cells(tmp_path, two_app_company):
    two_app_company.software = [two_app_company.software[0]]
    schemas = tmp_path / "app_schemas"
    schemas.mkdir()
    (schemas / "google_docs_mock.md").write_text("## State Schema\n| `documents` | object | Docs |\n")
    catalogs = SimpleNamespace(path=tmp_path, apps={"google_docs_mock": {}})
    all_rows = workflows.build_feature_matrix(two_app_company, catalogs, 10, 42)
    histogram = {cell_key(row["feature_cell"]): 9 for row in all_rows}
    preferred = cell_key(all_rows[-1]["feature_cell"])
    histogram[preferred] = 1
    result = workflows.build_feature_matrix(two_app_company, catalogs, 1, 42, {"cell_histogram": histogram})
    assert cell_key(result[0]["feature_cell"]) == preferred
    with pytest.raises(ValueError, match="distinct cells"):
        workflows.build_feature_matrix(two_app_company, catalogs, 11, 42)


def test_design_requires_distinct_assigned_cells_and_returns_them(root, two_app_company, workflow):
    author = Author(workflow)
    _, generated, receipt, decision = design(root, two_app_company, author, selected=["release", "second"])
    assert receipt == author.receipt
    assert decision["feature_matrix"] == author.payload["feature_matrix"]
    assert [w.feature_cell.model_dump() for w in generated] == [
        row["feature_cell"] for row in author.payload["feature_matrix"]
    ]
    defs = author.schema["$defs"]
    assert "feature_cell" in defs["Workflow"]["required"]
    assert defs["FeatureCell"]["properties"]["decision_type"]["enum"] == list(workflows.DECISION_TYPES)


def duplicate(output):
    output[1]["feature_cell"] = copy.deepcopy(output[0]["feature_cell"])
    output[1]["feature_cell"]["collections"].reverse()


@pytest.mark.parametrize(
    "mutate,match",
    [
        (duplicate, "duplicate feature_cell"),
        (lambda out: out[0]["feature_cell"]["collections"].append("other_app.tickets"), "company app"),
        (lambda out: out[0]["feature_cell"].update(decision_type="invent-a-type"), "vocabulary"),
    ],
)
def test_invalid_cells_follow_model_output_invalid_path(root, two_app_company, workflow, mutate, match):
    author = Author(workflow, mutate)
    with pytest.raises(ModelOutputInvalid, match=match) as caught:
        design(root, two_app_company, author)
    assert caught.value.receipt == author.receipt
    assert caught.value.raw["workflows"][0]["feature_cell"]


@pytest.mark.parametrize("invalid", [False, True])
def test_prepare_passes_matrix_and_records_duplicate_rejection(
    root, two_app_company, workflow, monkeypatch, invalid
):
    directory, state, path = setup_job(root, two_app_company, workflow)
    (path / "workflows.json").unlink()
    state["config"]["design"]["feature_matrix"] = True
    state["config"]["generation"]["max_revisions"] = 0
    job = state["jobs"][two_app_company.id]
    job["expansion_count"] = 2
    author = Author(workflow, duplicate if invalid else None)
    author.config = state["config"]
    monkeypatch.setattr(pipeline, "Models", lambda *args: author)
    if invalid:
        with pytest.raises(pipeline.DesignRejected, match="duplicate feature_cell"):
            pipeline.prepare(root, directory, state, job)
        assert not (path / "workflows.json").exists()
        issue = read(path / "expansion-issues-0-0.json")
        assert "duplicate feature_cell" in issue["errors"]
    else:
        pipeline.prepare(root, directory, state, job)
        saved = read(path / "workflows.json")
        assert len({cell_key(w["feature_cell"]) for w in saved}) == 2
        assert read(path / "design-decision-0-0.json")["feature_matrix"] == author.payload["feature_matrix"]
    assert len(author.payload["feature_matrix"]) == 2


def test_frozen_prepare_keeps_legacy_expand_path(root, company, workflow, monkeypatch):
    directory, state, path = setup_job(root, company, workflow)
    state["config"]["design"].pop("feature_matrix", None)
    (path / "workflows.json").unlink()
    write(directory / "run.json", state)
    called = []

    def expand(*args, **kwargs):
        called.append(True)
        return [workflow], {"model": "codex/fake-author", "job": "expand"}

    monkeypatch.setattr(workflows, "expand", expand)
    pipeline.prepare(root, directory, state, state["jobs"][company.id])
    assert called == [True]
    assert "feature_matrix" not in read(directory / "run.json")["config"]["design"]
    assert read(path / "workflows.json") == [workflow.model_dump()]


def test_histogram_covers_full_portfolio_and_the_summary_does_not_repeat_it(root, two_app_company, workflow):
    """The summary carried a second copy of the counters: 11 KB of identical JSON per prompt."""
    _, generated, _, _ = design(root, two_app_company, Author(workflow))
    result = summarize(generated, limit=1)
    assert result["shown_workflows"] == 1
    assert result["work"][0]["feature_cell"]
    # The histogram counts every accepted task; the prose sample is the only thing bounded.
    assert sum(cell_histogram(generated).values()) == 2
    assert not {"cell_histogram", "app_usage", "sector_counts", "assigned_app_history"} & set(result)
    assert summarize(list(reversed(generated)), limit=1) == result


def test_coverage_report_includes_frozen_portfolio_cells(root, two_app_company, workflow, monkeypatch):
    _, generated, _, _ = design(root, two_app_company, Author(workflow))
    state = {"id": "fake", "config": {"design": {"feature_matrix": True}}, "entries": [], "companies": {}}
    monkeypatch.setattr(pipeline, "chosen", lambda *args: [])
    monkeypatch.setattr(pipeline, "load_history", lambda *args: generated)
    result = pipeline.coverage(root, state, Catalogs(root / "catalogs"))
    assert result["cell_histogram"] == cell_histogram(generated)
    assert "cell_histogram" not in result["portfolio"]
    assert len(result["company_feature_cells"][two_app_company.id]) == 2


@pytest.mark.skipif(
    "feature_cell" not in SharedWorkflow.model_fields,
    reason="Requires the schemas.py and report.py owner patches supplied in the feature-matrix handoff",
)
def test_publish_reload_report_and_next_company_avoid_accepted_cells(
    root, two_app_company, workflow, monkeypatch
):
    directory, state, path = setup_job(root, two_app_company, workflow)
    (path / "workflows.json").unlink()
    state["config"]["design"]["feature_matrix"] = True
    state["targets"]["tasks"] = 2
    job = state["jobs"][two_app_company.id]
    job["expansion_count"] = 2
    author = Author(workflow)
    author.config = state["config"]
    monkeypatch.setattr(pipeline, "Models", lambda *args: author)
    pipeline.prepare(root, directory, state, job)
    # Reload through the same shared model used by fresh-session review and export.
    generated = [SharedWorkflow.model_validate(raw) for raw in read(path / "workflows.json")]

    class MatrixCritic(Critic):
        def call(self, *args, **kwargs):
            result, receipt = super().call(*args, **kwargs)
            result.tasks.append(result.tasks[0].model_copy(update={"workflow_id": "example_second"}))
            return result, receipt

    monkeypatch.setattr(pipeline, "Models", MatrixCritic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, two_app_company.id, None)
    write(directory / "run.json", state)
    expected = cell_histogram(generated)
    assert build_report(root, directory, state)["coverage"]["cell_histogram"] == expected
    report = export(root, directory, state)
    assert (
        report["coverage"]["difficulty"]["counts"]
        == workflows.difficulty_distribution(w.model_dump() for w in generated)["counts"]
    )
    assert report["coverage"]["corpus_difficulty"]["total"] == 2
    assert "## Run difficulty" in (directory / "REPORT.md").read_text()
    assert "## Corpus difficulty" in (directory / "REPORT.md").read_text()
    persisted_schema = read(directory / "schemas/workflow.schema.json")
    assert (
        persisted_schema["properties"]["difficulty"]
        == SharedWorkflow.model_json_schema()["properties"]["difficulty"]
    )
    assert "difficulty" not in persisted_schema["required"]
    cov = pipeline.coverage(root, state, Catalogs(directory / "catalogs"))
    assert cov["cell_histogram"] == expected

    future = root / "runs" / "future"
    manifest = freeze_history(root, future)
    history = load_history(future, {"portfolio": manifest})
    assert cell_histogram(history) == expected
    two_app_company.id = "later-company"
    matrix = workflows.build_feature_matrix(
        two_app_company, Catalogs(directory / "catalogs"), 2, 42, {"portfolio": summarize(history)}
    )
    assert not expected.keys() & {cell_key(row["feature_cell"]) for row in matrix}


@pytest.mark.parametrize("flag", [None, False])
def test_frozen_absent_or_disabled_flag_preserves_legacy_design(root, company, workflow, flag):
    author = Author(workflow, enabled=flag)
    if flag is None:
        author.config["design"].pop("feature_matrix")
    design(root, company, author)
    assert "feature_matrix" not in author.payload
    assert "feature_cell" not in author.schema["$defs"]["Workflow"]["properties"]


@pytest.mark.parametrize("flag", [None, False, True, "true"])
def test_new_run_freezes_default_and_validates_flag(root, monkeypatch, flag):
    config = tomllib.loads((root / "config.toml").read_text())
    config.setdefault("design", {}).pop("feature_matrix", None)
    if flag is not None:
        config["design"]["feature_matrix"] = flag
    monkeypatch.setattr(pipeline, "load_config", lambda _: copy.deepcopy(config))
    if flag == "true":
        with pytest.raises(ValueError, match="feature_matrix must be a boolean"):
            pipeline.new_run(root, 1, 1)
        assert not (root / "runs").exists()
    else:
        directory = pipeline.new_run(root, 1, 1)
        assert read(directory / "run.json")["config"]["design"]["feature_matrix"] is (flag is not False)


def test_missing_collection_schema_fails_before_model(root, company, workflow):
    author = Author(workflow)
    with pytest.raises(ValueError, match="catalog app collections"):
        design(root, company, author)
    assert not hasattr(author, "payload")


def test_deviating_from_assigned_cell_is_accepted_and_recorded_not_rejected(root, two_app_company, workflow):
    """Assigned rows steer; a distinct valid cell the author chose instead must not cost the company."""
    from company_envs.workflows import validate_feature_cells

    cells = validate_feature_cells(
        [
            workflow.model_copy(
                update={
                    "feature_cell": {
                        "collections": ["google_docs_mock.documents"],
                        "decision_type": "correct-records",
                    }
                }
            )
        ],
        [
            {
                "task_index": 1,
                "feature_cell": {"collections": ["slack_mock.messages"], "decision_type": "plan-schedule"},
            }
        ],
        ["google_docs_mock.documents", "slack_mock.messages"],
    )
    assert cells["deviations"] and cells["chosen"] and cells["assigned"]


@pytest.mark.parametrize("count", [1, 2, 7, 10, 20])
def test_difficulty_targets_round_to_the_batch_size(root, two_app_company, count):
    rows = workflows.build_feature_matrix(two_app_company, Catalogs(root / "catalogs"), count, 42)
    counts = Counter(row["difficulty"] for row in rows)
    assert sum(counts.values()) == count
    assert all(
        abs(counts[level] - count * share) < 1 for level, share in workflows.difficulty_shares().items()
    )
    if count == 10:
        assert counts == {"easy": 3, "medium": 4, "hard": 3}


def test_configured_mix_reaches_author_and_a_different_label_is_recorded(root, two_app_company, workflow):
    author = Author(workflow, lambda out: out[0].update(difficulty="hard"))
    author.config["generation"]["difficulty_mix"] = {"easy": 100, "medium": 0, "hard": 0}
    author.config["generation"]["difficulty_required"] = True
    _, generated, _, decision = design(
        root,
        two_app_company,
        author,
        prompt="Return difficulty on every workflow.",
        selected=["release", "second"],
    )
    assert author.payload["difficulty_mix"] == {"easy": 1, "medium": 0, "hard": 0}
    assert all(row["difficulty"] == "easy" for row in author.payload["feature_matrix"])
    field = author.schema["$defs"]["Workflow"]["properties"]["difficulty"]
    assert field == {"type": "string", "enum": ["easy", "medium", "hard"]}
    assert generated[0].difficulty == "hard"
    assert decision["feature_validation"]["difficulty"]["deviations"] == [
        {"workflow_id": "example_release", "assigned": "easy", "chosen": "hard"}
    ]
    # Fixed outline assignments survive a different output order.
    result = workflows.validate_feature_cells(
        list(reversed(generated)),
        author.payload["feature_matrix"],
        workflows.company_collections(two_app_company, Catalogs(root / "catalogs")),
    )
    assert result["difficulty"] == decision["feature_validation"]["difficulty"]


def test_config_requires_labels_but_frozen_configs_still_allow_omission(root, two_app_company, workflow):
    author = Author(workflow, lambda out: [w.pop("difficulty") for w in out])
    author.config["generation"]["difficulty_required"] = True
    with pytest.raises(ModelOutputInvalid, match="requires difficulty"):
        design(root, two_app_company, author, prompt="Return difficulty on every workflow.")
    author.config["generation"]["difficulty_required"] = False
    _, generated, _, _ = design(root, two_app_company, author)
    assert all(w.difficulty is None for w in generated)
    assert "difficulty" not in author.schema["$defs"]["Workflow"]["properties"]


@pytest.mark.parametrize(
    "mix",
    [
        {},
        [30, 40, 30],
        {"easy": 30, "medium": 70},
        {"easy": -1, "medium": 40, "hard": 61},
        {"easy": True, "medium": 40, "hard": 30},
        {"easy": "30", "medium": 40, "hard": 30},
        {"easy": 0, "medium": 0, "hard": 0},
        {"easy": float("nan"), "medium": 40, "hard": 30},
        {"easy": float("inf"), "medium": 40, "hard": 30},
    ],
)
def test_invalid_mix_fails_before_author_call(root, two_app_company, workflow, mix):
    author = Author(workflow)
    author.config["generation"]["difficulty_mix"] = mix
    with pytest.raises(ValueError, match="generation.difficulty_mix"):
        design(root, two_app_company, author)
    assert not hasattr(author, "payload")


DIFFICULTY_CASES = [
    (2, 0.25, 0.5, "correct-records", "easy"),
    (3, 0.5, 0.99, "respond-to-customer", "easy"),
    (2, 1, 1, "correct-records", "medium"),
    (2, 8, 16, "reconcile-discrepancy", "medium"),
    (3, 7, 16, "reconcile-discrepancy", "medium"),
    (3, 8, 16, "correct-records", "medium"),
    (3, 8, 16, None, "medium"),
    (3, 8, 16, "reconcile-discrepancy", "hard"),
    (4, 16, 24, "allocate-scarce-resource", "hard"),
    (5, 24, 40, "negotiate-terms", "hard"),
]


@pytest.fixture
def difficulty_run(tmp_path):
    entries = []
    for index, (workers, low, high, decision, _) in enumerate(DIFFICULTY_CASES):
        raw = {
            "id": f"task_{index}",
            "worker_ids": [f"worker_{n}" for n in range(workers)],
            "estimated_human_effort": {"minimum_hours": low, "maximum_hours": high},
            "feature_cell": {"decision_type": decision},
        }
        path = f"data/workflows/{digest(raw)}.json"
        write(tmp_path / path, raw)
        entries.append({"workflow_id": raw["id"], "workflow_path": path, "verdict": "accept"})
    write(tmp_path / "runs/fixture/dataset.json", {"workflows": entries})
    return tmp_path


def test_backfill_measurement_preserves_sources_and_survives_rerun(difficulty_run):
    root = difficulty_run
    source_bytes = {p: p.read_bytes() for p in (root / "data/workflows").glob("*.json")}
    before = corpus_difficulty(root)
    assert before["counts"] == {"easy": 0, "medium": 0, "hard": 0, "unlabeled": 10}
    result = label_difficulty(root, "fixture")
    assert result["counts"] == {"easy": 2, "medium": 5, "hard": 3, "unlabeled": 0}
    assert result["difference_percentage_points"] == pytest.approx({"easy": -10, "medium": 10, "hard": 0})
    assert result["distance_percentage_points"] == pytest.approx(10)
    path = root / "runs/fixture/dataset.json"
    dataset = read(path)
    assert [e["difficulty"] for e in dataset["workflows"]] == [case[-1] for case in DIFFICULTY_CASES]
    assert all(e["difficulty_source"] == "heuristic-v1" for e in dataset["workflows"])
    saved = path.read_bytes()
    assert label_difficulty(root, "fixture") == result
    assert path.read_bytes() == saved
    assert all(p.read_bytes() == raw for p, raw in source_bytes.items())
    assert corpus_difficulty(root) == result
    # A duplicate export does not inflate corpus counts.
    write(root / "runs/second/dataset.json", dataset)
    assert corpus_difficulty(root) == result


def test_backfill_keeps_authored_labels_ignores_rejections_and_uses_config(difficulty_run):
    root = difficulty_run
    path = root / "runs/fixture/dataset.json"
    dataset = read(path)
    entry = dataset["workflows"][0]
    raw = read(root / entry["workflow_path"])
    raw["difficulty"] = "hard"
    write(root / entry["workflow_path"], raw)
    dataset["workflows"].append({"workflow_id": "rejected", "verdict": "reject", "workflow_path": "absent"})
    write(path, dataset)
    write(
        root / "runs/fixture/run.json",
        {"config": {"generation": {"difficulty_mix": {"easy": 1, "medium": 1, "hard": 2}}}},
    )
    result = label_difficulty(root, "fixture")
    assert result["target"] == {"easy": 0.25, "medium": 0.25, "hard": 0.5}
    assert result["total"] == 10
    entry = read(path)["workflows"][0]
    assert entry["difficulty"] == "hard" and entry["difficulty_source"] == "authored"
    assert "difficulty" not in read(path)["workflows"][-1]


def test_changed_sources_drop_stale_backfill_and_locked_runs_refuse_writes(difficulty_run):
    root = difficulty_run
    label_difficulty(root, "fixture")
    dataset = read(root / "runs/fixture/dataset.json")
    source = root / dataset["workflows"][0]["workflow_path"]
    raw = read(source)
    raw["estimated_human_effort"]["maximum_hours"] = 2
    write(source, raw)
    assert corpus_difficulty(root)["counts"]["unlabeled"] == 1
    with run_lock(root / "runs/fixture"), pytest.raises(RuntimeError, match="owns this run"):
        label_difficulty(root, "fixture")
    assert label_difficulty(root, "fixture")["counts"]["medium"] == 6


@pytest.mark.parametrize("run_id", ["", "../outside", ".", "..", "/tmp/outside"])
def test_backfill_rejects_paths_outside_a_run(tmp_path, run_id):
    with pytest.raises(ValueError, match="run_id"):
        label_difficulty(tmp_path, run_id)


def test_missing_run_and_missing_source_do_not_create_backfill(tmp_path, difficulty_run):
    with pytest.raises(FileNotFoundError):
        label_difficulty(tmp_path, "missing")
    assert not (tmp_path / "runs/missing").exists()
    path = difficulty_run / "runs/fixture/dataset.json"
    before = path.read_bytes()
    (difficulty_run / read(path)["workflows"][-1]["workflow_path"]).unlink()
    with pytest.raises(FileNotFoundError):
        label_difficulty(difficulty_run, "fixture")
    assert path.read_bytes() == before


def test_empty_and_invalid_difficulty_distributions():
    empty = workflows.difficulty_distribution([])
    assert empty["total"] == 0 and empty["distance_percentage_points"] is None
    assert all(v is None for v in empty["shares"].values())
    with pytest.raises(ValueError, match="difficulty"):
        workflows.difficulty_distribution([{"difficulty": "expert"}])
    assert workflows.infer_difficulty({}) == "medium"
    assert (
        workflows.infer_difficulty(
            {
                "worker_ids": ["a", "b"],
                "estimated_human_effort": {"maximum_hours": 0.5},
                "feature_cell": {"decision_type": "negotiate-terms"},
            }
        )
        == "medium"
    )


def test_backfill_survives_verified_report_export_without_changing_reviewed_design(
    root, company, workflow, monkeypatch
):
    directory, state, _ = setup_job(root, company, workflow)
    monkeypatch.setattr(pipeline, "Models", Critic)
    monkeypatch.setattr(pipeline, "nearest", lambda *args: [])
    pipeline.publish(root, directory, state, company.id, None)
    write(directory / "run.json", state)
    before = export(root, directory, state)
    assert before["coverage"]["difficulty"]["counts"]["unlabeled"] == 1
    path = root / before["workflows"][0]["workflow_path"]
    original = path.read_bytes()
    label_difficulty(root, state["id"])
    after = export(root, directory, state)
    assert after["validation_errors"] == {}
    assert after["coverage"]["difficulty"]["counts"]["medium"] == 1
    assert after["coverage"]["corpus_difficulty"]["counts"]["medium"] == 1
    assert after["workflows"][0]["difficulty_source"] == "heuristic-v1"
    assert path.read_bytes() == original
    assert "| medium | 1 | 100.0% | 40.0% | +60.0 |" in (directory / "REPORT.md").read_text()


def test_record_counts_steer_cells_away_from_rosters_and_thin_collections(root, two_app_company):
    """Mining drew its decisive collections by digest, which favoured nothing that could carry a
    decision. Counts make density the last steer before the hash, and change nothing without them.
    """
    catalogs = Catalogs(root / "catalogs")
    collections = workflows.company_collections(two_app_company, catalogs)
    sheets = next(c for c in collections if c.endswith(".sheets"))
    users = next(c for c in collections if c.endswith(".users"))
    counts = {c: 1 for c in collections} | {sheets: 400, users: 900}

    # A roster is ranked behind a thin business collection even when the roster is far larger.
    assert workflows.decision_surface_rank(counts, [sheets]) < workflows.decision_surface_rank(
        counts, [users]
    )
    # The thinnest side of a bundle decides, and a dense bundle beats a one-record one.
    assert workflows.decision_surface_rank(counts, [sheets, sheets]) < workflows.decision_surface_rank(
        counts, [sheets, next(c for c in collections if counts[c] == 1)]
    )
    # Without counts the term is constant, so the unseeded path keeps its exact former ordering.
    assert workflows.decision_surface_rank({}, [users]) == workflows.decision_surface_rank({}, [sheets]) == 0
    assert workflows.build_feature_matrix(two_app_company, catalogs, 6, 42) == workflows.build_feature_matrix(
        two_app_company, catalogs, 6, 42, available_collections=list(collections)
    )

    # With counts the assigned cell stands on records; without them it drew a one-record collection.
    def thinnest(available):
        row = workflows.build_feature_matrix(
            two_app_company, catalogs, 1, 42, available_collections=available
        )[0]
        return min(counts[c] for c in row["feature_cell"]["collections"])

    assert thinnest(list(collections)) == 1 < thinnest(counts)


def test_fallback_schema_keys_exclude_interface_state(tmp_path, two_app_company):
    """A schema with no machine-readable record table must not offer UI state as a decisive cell."""
    two_app_company.software = [two_app_company.software[0]]
    schemas = tmp_path / "app_schemas"
    schemas.mkdir()
    (schemas / "google_docs_mock.md").write_text(
        "## State Schema\n| `documents` | object | Docs |\n| `sortConfig` | object | Sort |\n"
        "| `selectedItems` | array | Selection |\n"
    )
    catalogs = SimpleNamespace(path=tmp_path, apps={"google_docs_mock": {}})
    assert workflows.company_collections(two_app_company, catalogs) == ["google_docs_mock.documents"]


def test_a_decisive_app_nobody_on_the_team_holds_is_rejected(two_app_company):
    """worker_apps.check_binding verifies the manager is EXCLUDED from a decisive app and never that
    anyone is included, so a decisive collection held by nobody passed every gate and the work it
    names could not be done. Latent rather than live: 0 of the 178 accepted tasks with a cell and
    per-worker apps has an unheld decisive app. The manager not holding one stays legal by design."""
    cell = SimpleNamespace(collections=["google_sheets_mock.sheets", "google_docs_mock.documents"])
    workflow = SimpleNamespace(
        id="w1",
        manager_id="boss",
        feature_cell=cell,
        contributions=[
            SimpleNamespace(worker_id="boss", apps=["google_docs_mock"]),
            SimpleNamespace(worker_id="analyst", apps=["google_sheets_mock"]),
        ],
    )
    workflows.check_contribution_apps(two_app_company, workflow)  # the specialist holds the cell
    workflow.contributions[1].apps = ["google_docs_mock"]
    with pytest.raises(ValueError, match=r"nobody on the team holds \['google_sheets_mock'\]"):
        workflows.check_contribution_apps(two_app_company, workflow)


def test_record_counts_and_not_the_id_index_decide_the_matrix_surface(root, two_app_company, workflow):
    """The mining surface was derived from an index that counts only dicts carrying an "id", so
    slack_mock.users (keyed by userId), slack_mock.messages (a map of lists) and amazon_mock.wishlist
    (bare ids) were withheld from every mined company: 581 of the 3,695 collections in the 60 seeded
    worlds, including every slack collection in every world. Measured with shape-aware counts, the
    matrix surface grows from 1,118 to 1,523 collections (+36%), median 18 to 26, none lost."""
    author = Author(workflow)
    blind = {
        "google_sheets_mock": {"sheets": [], "charts": []},
        "google_docs_mock": {"documents": [{"id": "doc1"}], "users": []},
    }
    seeded = {
        "app_index": blind,
        "record_counts": {"google_sheets_mock.sheets": 31, "google_docs_mock.documents": 268},
    }
    # The double returns the scheduled fixture workflow, which the frozen digital run rejects; the
    # payload the designer was handed is built before the call, so the surface is still observable.
    with pytest.raises(ModelOutputInvalid, match="execution mode differs from the frozen run"):
        design(root, two_app_company, author, seeded_world=seeded, selected=["release", "second"])
    # The payload is built before the call, so the surface the matrix drew from is observable.
    used = {c for row in author.payload["feature_matrix"] for c in row["feature_cell"]["collections"]}
    assert used == {"google_sheets_mock.sheets", "google_docs_mock.documents"}
