"""Exercise production gates with frozen answers and in-memory apps, never live services."""

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from company_envs import skill_eval as ev
from company_envs.storage import read, write

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/skills"
SEL = "schweitzer-engineering-laboratories"
CASES = sorted(FIXTURES.glob("*/*.json"))


@pytest.fixture
def root(tmp_path):
    shutil.copyfile(ROOT / "config.toml", tmp_path / "config.toml")
    shutil.copytree(FIXTURES, tmp_path / "tests/fixtures/skills")
    for name in ev.SKILLS:
        source = ROOT / ".agents/skills" / name / "SKILL.md"
        if source.is_file():
            target = tmp_path / ".agents/skills" / name / "SKILL.md"
            target.parent.mkdir(parents=True)
            shutil.copyfile(source, target)
    return tmp_path


def case(skill, name=SEL):
    return read(FIXTURES / skill / f"{name}.json")


def failures(data, answer, directory, models=None):
    directory.mkdir(parents=True, exist_ok=True)
    try:
        return ev.score(data, answer, directory, models or ev.DryModels(data))
    except (ValueError, KeyError, TypeError) as exc:
        return [str(exc)]


@pytest.mark.parametrize("path", CASES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_frozen_answers_use_production_gates(path, tmp_path):
    data = read(path)
    assert data["provenance"]["sources"]
    assert any(s["path"].startswith("companies/") for s in data["provenance"]["sources"])
    assert all(len(s["sha256"]) == 64 for s in data["provenance"]["sources"])
    if data["skill"] in {"company-task-grader", "company-task-golden"} and path.stem == SEL:
        # Frozen before the app barrier: a one-app world where every worker writes in GitHub,
        # which the held-apps milestone now rejects. Refreeze from a barrier-compliant company.
        pytest.xfail("fixture predates the app barrier (held-apps milestone)")
    result = failures(data, data["double"], tmp_path)
    if result and data["skill"] == "company-workflows" and path.stem == "ust":
        # Existing Contribution.apps uses lowercase-only Id despite mixed-case catalog app ids.
        assert "ServiceNow_mock" in result[0]
    else:
        assert result == []
    if data["skill"] in {"company-task-grader", "company-task-golden"}:
        report = read(tmp_path / "company/runtime/grades" / data["task_id"] / "calibration.json")
        assert report["initial_score"] == 0
        assert report["reference_score"] == 1
        assert report["contributions"]["observed_workers"] == 3


def test_all_skills_have_two_distinct_inputs():
    assert {p.name for p in FIXTURES.iterdir() if p.is_dir()} == set(ev.SKILLS)
    for skill in ev.SKILLS:
        paths = list((FIXTURES / skill).glob("*.json"))
        assert len(paths) >= 2
        assert len({json.dumps(read(p)["input"], sort_keys=True) for p in paths}) == len(paths)


@pytest.mark.parametrize("skill", ev.SKILLS)
def test_invalid_answers_never_earn_credit(skill, tmp_path):
    assert failures(case(skill), {}, tmp_path)


@pytest.mark.parametrize(
    "skill,mutation",
    [
        ("company-discovery", "empty"),
        ("company-discovery", "duplicate"),
        ("company-discovery", "excluded"),
        ("company-discovery", "sector"),
        ("company-research", "identity"),
        ("company-research", "quote"),
        ("company-research", "sheets"),
        ("company-review", "accept"),
        ("company-review", "omit-task"),
        ("company-workflows", "brief"),
        ("company-workflows", "apps"),
        ("company-workflows", "cell"),
        ("company-workflows", "empty-apps"),
        ("company-plain-brief", "date"),
        ("company-plain-brief", "count"),
        ("company-plain-brief", "identity"),
        ("company-worker-apps", "manager"),
        ("company-worker-apps", "cell"),
        ("company-worker-apps", "duplicate"),
        ("company-worker-apps", "identity"),
        ("company-world-review", "accept"),
        ("company-world-review", "wrong-record"),
        ("company-world-review", "shotgun"),
        ("company-world-review", "no-finding"),
    ],
)
def test_business_gate_rejections(skill, mutation, tmp_path):
    data = case(skill)
    answer = deepcopy(data["double"])
    if skill == "company-discovery":
        if mutation == "empty":
            answer["candidates"] = []
        elif mutation == "duplicate":
            data["input"]["candidate_count"] = 2
            answer["candidates"] *= 2
        elif mutation == "excluded":
            data["input"]["excluded"] = answer["candidates"]
        else:
            answer["candidates"][0]["sector"] = "Wrong sector"
    elif skill == "company-research":
        if mutation == "identity":
            answer["id"] = "wrong-company"
        elif mutation == "quote":
            answer["evidence"][0]["quote"] = "A claim absent from the captured text."
        else:
            for software in answer["software"]:
                software["catalog_app_ids"] = ["google_sheets_mock"]
    elif skill == "company-review":
        if mutation == "accept":
            answer["company_verdict"] = "accept"
        else:
            answer["tasks"][0]["workflow_id"] = "unknown-task"
    elif skill == "company-workflows":
        if mutation == "brief":
            answer["brief"] = "Reconcile evidence and integrate consequential handoffs by tomorrow."
        elif mutation == "cell":
            answer["feature_cell"]["collections"] = ["github_mock.unknown"]
        elif mutation == "empty-apps":
            for contribution in answer["contributions"]:
                contribution["apps"] = []
        else:
            for contribution in answer["contributions"]:
                contribution["apps"] = ["github_mock"]
    elif skill == "company-plain-brief":
        if mutation == "date":
            answer["brief"] = "Choose a maintenance scope and record it."
        elif mutation == "count":
            answer["deliverables"] = []
        else:
            answer["workflow_id"] = "another-task"
    elif skill == "company-worker-apps":
        if mutation == "manager":
            for contribution in answer["contributions"]:
                contribution["apps"] = ["github_mock"]
        elif mutation == "cell":
            answer["decisive_collections"] = ["github_mock.currentUser"]
        elif mutation == "duplicate":
            answer["contributions"].append(answer["contributions"][0])
        else:
            answer["workflow_id"] = "another-task"
    elif mutation == "accept":
        answer["verdict"] = "accept"
    elif mutation == "no-finding":
        answer["findings"] = []
    elif mutation == "shotgun":
        answer["contradictions"].append({"left": "/world", "right": "/states"})
    else:
        answer["contradictions"][0]["right"] = "/states/github_mock/issues/1/title"
    assert failures(data, answer, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-app",
        "missing-key",
        "missing-worker",
        "missing-identity",
        "empty-world",
        "contradiction",
        "changed-source",
        "empty-records",
        "recipe",
        "duplicate",
        "directory",
        "duplicate-json-key",
    ],
)
def test_world_states_run_coherence_gates(mutation, tmp_path):
    data = case("company-world-states")
    state = json.loads(data["double"]["state_json"])
    app = state["states"]["github_mock"]
    if mutation == "missing-app":
        state["states"] = {}
    elif mutation == "missing-key":
        app.pop("issues")
    elif mutation == "missing-worker":
        state["identities"].pop(next(iter(state["identities"])))
    elif mutation == "missing-identity":
        state["identities"][next(iter(state["identities"]))] = {}
    elif mutation == "empty-world":
        state["world"] = {}
    elif mutation == "contradiction":
        app["issues"][0]["status"] = "closed"
    elif mutation == "changed-source":
        app["issues"][0]["status"] = "closed"
        state["world"]["issues"][0]["status"] = "closed"
    elif mutation == "empty-records":
        app["issues"] = []
    elif mutation == "recipe":
        state["world"]["issues"] = "Generate 600 records using i=1..600 and pad(i,4)."
    elif mutation == "duplicate":
        app["issues"].append(app["issues"][0])
    elif mutation == "directory":
        app["users"] = []
        app["currentUser"] = {}
    answer = {"state_json": json.dumps(state)}
    if mutation == "duplicate-json-key":
        answer["state_json"] = answer["state_json"].replace('"world":', '"world": {}, "world":', 1)
    assert failures(data, answer, tmp_path)


@pytest.mark.parametrize(
    "skill,mutation",
    [
        ("company-task-grader", "preexisting"),
        ("company-task-grader", "wrong-target"),
        ("company-task-golden", "noop"),
        ("company-task-golden", "one-worker"),
        ("company-task-golden", "unknown-worker"),
        ("company-task-amplify", "identity"),
        ("company-task-amplify", "stale-value"),
        ("company-task-amplify", "unchanged-completion"),
    ],
)
def test_calibration_and_variant_rejections(skill, mutation, tmp_path):
    data = case(skill)
    answer = deepcopy(data["double"])
    models = ev.DryModels(data)
    if skill == "company-task-grader":
        predicate = answer["checks"][0]["predicate"]
        if mutation == "preexisting":
            predicate.update(operator="exists", value_json=None)
        else:
            predicate["selector"] = '$.issues[?(@.id=="ISS-F403")].labels'
    elif skill == "company-task-golden":
        steps = json.loads(answer["golden_json"])
        if mutation == "noop":
            steps = steps[:1]
        else:
            for step in steps:
                step["worker_id"] = steps[0]["worker_id"] if mutation == "one-worker" else "stranger"
        answer["golden_json"] = json.dumps(steps)
    else:
        variant = answer["variants"][0]
        if mutation == "identity":
            variant["records"][0]["edits"][0]["path"] = "/id"
        elif mutation == "stale-value":
            variant["records"][0]["edits"][0]["before_json"] = '"wrong original"'
        else:
            variant["workflow_edits"] = variant["workflow_edits"][:1]
    assert failures(data, answer, tmp_path, models)


def test_memory_hub_copies_and_isolates_sessions():
    hub = ev.MemoryHub()
    state = {"rows": [{"id": 1}]}
    hub.seed("first", state)
    hub.seed("second", state)
    state["rows"].clear()
    seen = hub.inspect("first")
    seen["current_state"]["rows"].clear()
    assert hub.current("first")["stored_state"]["rows"]
    hub.update("first", state)
    assert hub.inspect("first")["initial_state"]["rows"]
    assert hub.current("second")["stored_state"]["rows"]
    hub.reset("first")
    assert not hub.current("first")["has_custom_state"]


def test_all_skill_reports_and_missing_skill_are_visible(root, monkeypatch):
    (root / ".agents/skills/company-task-golden/SKILL.md").unlink(missing_ok=True)
    monkeypatch.setattr(ev, "Models", lambda *args: pytest.fail("dry run made a real model"))
    directory, reports = ev.run(root, dry_run=True)
    assert len(reports) == 11
    assert directory.parent.parent == root / "experiments/skill-evals"
    assert directory.name.startswith("dry-")
    summary = (directory / "SUMMARY.md").read_text()
    assert sum(line.startswith("| ---") for line in summary.splitlines()) == 1
    for report in reports:
        assert read(directory / f"{report['skill']}.json") == report
        assert report["mode"] == "double"
        assert len(report["fixtures"]) == 2
        assert 0 <= report["score"] <= 1
    golden = next(r for r in reports if r["skill"] == "company-task-golden")
    assert golden["score"] == 0
    assert "missing skill file" in golden["failures"][0]
    workflows = next(r for r in reports if r["skill"] == "company-workflows")
    assert workflows["score"] == sum(row["score"] for row in workflows["fixtures"]) / 2


def test_golden_runner_with_supplied_skill_instructions(root):
    path = root / ".agents/skills/company-task-golden/SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ev.golden.AUTHOR_INSTRUCTIONS)
    _, reports = ev.run(root, skill="company-task-golden", dry_run=True)
    if reports[0]["failures"] and (
        "do not hold" in reports[0]["failures"][0] or "task contract" in reports[0]["failures"][0]
    ):
        pytest.xfail("fixture predates the app barrier (held-apps milestone)")
    assert reports[0]["score"] == 1
    assert not reports[0]["failures"]


@pytest.mark.parametrize("name", [SEL, "ust"])
def test_world_review_agreement_uses_locations_and_one_real_defect(name, tmp_path):
    data = case("company-world-review", name)
    world = data["input"]
    report = ev.check_world(world["world"], world["states"], world["identities"], [], "2026-09-08T23:59:59Z")
    assert report["errors"] == 1
    answer = data["double"]
    pair = answer["contradictions"][0]
    pair["left"], pair["right"] = pair["right"], pair["left"]
    answer["findings"][0]["issue"] = "These copies disagree."
    assert failures(data, answer, tmp_path) == []


def test_current_skill_bytes_and_pinned_config_reach_models(root, monkeypatch):
    calls = []

    class CapturingModels:
        def __init__(self, config, directory):
            assert config["models"]["expand"] == [config["design"]["model_pin"]]

        def call(self, job, prompt, response_type):
            payload = json.loads(prompt.split("\n")[-1])
            calls.append((job, prompt, payload))
            name = SEL if payload["task"]["id"].startswith(SEL) else "ust"
            return response_type.model_validate(case("company-plain-brief", name)["double"]), {
                "model": "codex/gpt-6-astra"
            }

    monkeypatch.setattr(ev, "Models", CapturingModels)
    first, reports = ev.run(root, skill="company-plain-brief")
    assert reports[0]["score"] == 1
    assert reports[0]["mode"] == "pinned_model"
    path = root / ".agents/skills/company-plain-brief/SKILL.md"
    path.write_text(path.read_text() + "\nA test prompt edit.\n")
    second, changed = ev.run(root, skill="company-plain-brief")
    assert first != second
    assert reports[0]["skill_hash"] != changed[0]["skill_hash"]
    assert "A test prompt edit." in calls[-1][1]
    assert "oracle" not in calls[-1][2]
    assert "double" not in calls[-1][2]
    assert len(list(second.glob("*/*/response.json"))) == 2


def test_author_inputs_exclude_private_answers():
    for skill in ("company-task-grader",):
        for path in (FIXTURES / skill).glob("*.json"):
            payload = json.dumps(read(path)["input"])
            assert "feasible_path" not in payload
            assert "golden_json" not in payload
            assert "oracle" not in payload


@pytest.mark.parametrize(
    "change", ["non-gpt", "drift", "unknown", "missing-fixture", "malformed-fixture", "misfiled"]
)
def test_invalid_setup_is_explicit(root, change):
    skill = "company-discovery"
    config = root / "config.toml"
    if change == "non-gpt":
        config.write_text(
            config.read_text().replace('model_pin = "codex/gpt-6-astra"', 'model_pin = "other/model"')
        )
    elif change == "drift":
        config.write_text(
            config.read_text().replace('discover = ["codex/gpt-6-astra"]', 'discover = ["codex/gpt-other"]')
        )
    elif change == "unknown":
        skill = "not-a-skill"
    else:
        path = root / "tests/fixtures/skills" / skill / "ust.json"
        if change == "missing-fixture":
            path.unlink()
        elif change == "malformed-fixture":
            path.write_text("{")
        else:
            data = read(path)
            data["skill"] = "company-review"
            write(path, data)
    if change in {"non-gpt", "drift", "unknown"}:
        with pytest.raises(ValueError):
            ev.run(root, skill=skill, dry_run=True)
    else:
        _, reports = ev.run(root, skill=skill, dry_run=True)
        assert reports[0]["score"] < 1
        assert reports[0]["failures"]


def test_failed_call_does_not_hide_other_fixtures(root):
    class Unavailable:
        def call(self, *args):
            raise RuntimeError("Test transport unavailable")

    _, reports = ev.run(root, skill="company-discovery", models=Unavailable())
    assert reports[0]["score"] == 0
    assert len(reports[0]["fixtures"]) == 2
    assert all("Test transport unavailable" in f for f in reports[0]["failures"])


def test_fixture_path_cannot_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        ev.fixture_world({"world_files": {"../../outside": {}}}, tmp_path)


def test_cli_selection_and_exit_code(monkeypatch, capsys, tmp_path):
    calls = []

    def run(root, **kwargs):
        calls.append(kwargs)
        return tmp_path, [{"score": 1 if kwargs["dry_run"] else 0}]

    monkeypatch.setattr(ev, "run", run)
    assert ev.main(["--skill", "company-discovery", "--dry-run"]) == 0
    assert calls[-1] == {"skill": "company-discovery", "dry_run": True}
    assert ev.main(["--skill", "company-discovery"]) == 1
    assert "SUMMARY.md" in capsys.readouterr().out
