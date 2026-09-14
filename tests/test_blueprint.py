"""A canonical checkpoint is reviewable, resumable, and independent of app population."""

import json

import pytest
from test_state_seed import FakeModels, app_state, core
from test_state_seed import exported as exported  # noqa: PLC0414

from company_envs.storage import digest, read, write
from company_envs.world import blueprint, state_seed
from company_envs.world.world_review import MaterialsOnly, WorkerMaterial, WorldReview, repair_materials


class CoreModels:
    def __init__(self):
        raw = core().model_dump()
        people = []
        for identity in raw["identities"]:
            person = json.loads(identity["user_json"])
            people.append(
                {
                    **person,
                    "id": identity["worker_id"],
                    "worker_id": identity["worker_id"],
                    "apps": ["demo_mock"],
                }
            )
        raw["entities_json"] = json.dumps(
            {
                "entities": people,
                "history": [
                    {
                        "id": "h1",
                        "date": "2026-08-01",
                        "apps": ["demo_mock"],
                        "event": "Opened the support desk",
                    }
                ],
            }
        )
        raw["materials"] = [
            {
                "worker_id": "w1",
                "path": "Documents/Forecast.xlsx",
                "content": "## Sheet: Orders\nQuantity,Price,Total\n2,15,=A2*B2",
            },
            {
                "worker_id": "w2",
                "path": "Documents/Policy.pdf",
                "content": "# Service policy\nReview the customer request before accepting a return.",
            },
        ]
        self.value = blueprint.Blueprint.model_validate(
            {
                **raw,
                "population_plan": [
                    {
                        "app_id": "demo_mock",
                        "collection": "tickets",
                        "target_records": 200,
                        "record_unit": "tickets",
                        "first_date": "2025-09-01",
                        "last_date": "2026-09-08",
                        "purpose": "Support history",
                        "canonical_sources": ["h1"],
                    }
                ],
            }
        )
        self.calls = []

    def call(self, job, prompt, response_type, **kwargs):
        self.calls.append(response_type)
        if response_type is blueprint.Blueprint:
            return self.value, {"call_id": "core"}
        if response_type is WorldReview:
            return WorldReview(verdict="accept", summary="Coherent", findings=[]), {"call_id": "review"}
        pytest.fail(f"Stage 2 unexpectedly requested {response_type}")


@pytest.fixture
def fresh(exported):
    root, folder = exported
    for path in (folder / "tasks").glob("*/*.json"):
        path.unlink()
    return root, folder


def test_core_checkpoint_renders_actual_documents_and_reuses_without_calls(fresh):
    from openpyxl import load_workbook

    root, folder = fresh
    models = CoreModels()
    result = blueprint.seed_core(root, folder, models=models)
    assert result["status"] == "core_reviewed", result
    assert models.calls == [blueprint.Blueprint, WorldReview]
    assert not list((folder / "world").glob("*.state.json"))
    desktop = folder / "world/desktop"
    assert (desktop / "w2/Documents/Policy.pdf").read_bytes().startswith(b"%PDF")
    book = load_workbook(desktop / "w1/Documents/Forecast.xlsx")
    assert book["Orders"]["C2"].value == "=A2*B2"
    assert blueprint.seed_core(root, folder, models=models)["calls_this_invocation"] == 0
    assert len(models.calls) == 2
    assert read(folder / "world/population.json")[0]["target_records"] == 200


@pytest.mark.parametrize("change", ["company", "material", "spec"])
def test_core_checkpoint_rejects_drift_before_another_call(fresh, change):
    root, folder = fresh
    models = CoreModels()
    blueprint.seed_core(root, folder, models=models)
    if change == "company":
        value = read(folder / "company.json")
        value["name"] = "Changed company"
        write(folder / "company.json", value)
    elif change == "spec":
        write(folder / "world-spec.json", {"minimum_history_records": 200})
    else:
        (folder / "world/materials/w2/Documents/Policy.pdf").write_text("Changed policy")
    with pytest.raises(ValueError, match="checkpoint"):
        blueprint.seed_core(root, folder, models=models)
    assert len(models.calls) == 2


def test_app_population_consumes_core_without_reauthoring(fresh):
    root, folder = fresh
    blueprint.seed_core(root, folder, models=CoreModels())
    models = FakeModels(None, app_state())
    result = state_seed.seed_world(root, folder, models=models, review_rounds=0)
    assert all(response is not state_seed.WorldCore for _, _, response in models.calls)
    assert (folder / "world/demo_mock.state.json").is_file()
    assert result["status"] == "seeded_review_failed"  # Fake output is below the 200-ticket target.
    prompts = [
        json.loads(prompt.split("\n", 1)[1])
        for _, prompt, schema in models.calls
        if schema is state_seed.AppStateResult
    ]
    ticket_calls = [p for p in prompts if "tickets" in p["named_collections"]]
    assert ticket_calls and all(p["population_targets"][0]["target_records"] == 200 for p in ticket_calls)
    seed = read(folder / "world/SEED.json")
    assert seed["population_shortfalls"]["demo_mock"][0]["target"] == 200
    assert result["calls"] == len(models.calls)
    manifest = read(folder / "MANIFEST.json")
    assert "MANIFEST.json" not in manifest["hashes"]
    assert all(
        digest((folder / path).read_bytes()) == expected for path, expected in manifest["hashes"].items()
    )


def test_reviewer_accept_cannot_override_missing_real_history(fresh):
    root, folder = fresh
    write(folder / "world-spec.json", {"minimum_history_records": 12, "history_months": 12})
    report = blueprint.seed_core(root, folder, models=CoreModels(), review_rounds=0)
    assert report["status"] == "core_needs_revision"
    assert not report["checks"]["ok"]
    with pytest.raises(ValueError, match="unaccepted"):
        blueprint.load_core(root, folder)


def test_material_repair_sends_only_cited_file_and_preserves_every_other_file():
    materials = [
        WorkerMaterial(worker_id="a", path="bad.md", content="old"),
        WorkerMaterial(worker_id="b", path="good.md", content="x" * 50000),
    ]

    class Model:
        def call(self, job, prompt, response_type):
            payload = json.loads(prompt.split("\n", 1)[1])
            assert [m["path"] for m in payload["previous_materials"]] == ["bad.md"]
            assert "x" * 1000 not in prompt
            return MaterialsOnly(
                materials=[WorkerMaterial(worker_id="a", path="bad.md", content="fixed")]
            ), {}

    result = repair_materials(
        materials, [{"target": "materials", "evidence": "a/bad.md"}], {}, Model(), "broad skill" * 1000
    )
    assert result[0].content == "fixed"
    assert result[1] is materials[1]


def test_static_directories_never_acquire_historical_shards():
    for collection in ["users", "channels", "contacts", "companies", "labels", "calendars", "undoStack"]:
        assert not state_seed.history_eligible(collection, [{"id": i} for i in range(100)])
    assert state_seed.history_eligible("emails", [{"id": 1}])


def test_core_honors_independent_review_vote_count(fresh):
    root, folder = fresh
    config = root / "config.toml"
    config.write_text(config.read_text().replace("[generation]", "[generation]\nreview_votes = 3"))
    models = CoreModels()
    result = blueprint.seed_core(root, folder, models=models)
    assert result["status"] == "core_reviewed"
    assert models.calls.count(WorldReview) == 3
    assert len(read(folder / "world/CORE-REVIEW.json")["rounds"][0]["ballots"]) == 3


def test_named_collections_and_timestamp_reference_are_valid(fresh):
    root, folder = fresh
    models = CoreModels()
    world = json.loads(models.value.entities_json)
    world["staff"] = world.pop("entities")
    models.value = models.value.model_copy(
        update={"entities_json": json.dumps(world), "reference_date": "2026-09-08T08:00:00-07:00"}
    )
    write(folder / "world-spec.json", {"reference_date": "2026-09-08", "minimum_entities": 2})
    assert blueprint.seed_core(root, folder, models=models)["status"] == "core_reviewed"


def test_no_op_repair_stops_and_resume_reviews_without_reauthoring(fresh):
    root, folder = fresh
    models = CoreModels()
    write(folder / "world-spec.json", {"prospect_accounts": 1})
    first = blueprint.seed_core(root, folder, models=models)
    assert first["status"] == "core_needs_revision"
    assert first["repair_rounds"] == 0
    assert first["review"] == "not_run"
    assert models.calls == [blueprint.Blueprint]
    world = read(folder / "world/world.json")
    world["accounts"] = [{"id": "a1", "status": "prospect", "apps": ["demo_mock"]}]
    write(folder / "world/world.json", world)
    result = blueprint.seed_core(root, folder, models=models, resume=True)
    assert result["status"] == "core_reviewed"
    assert models.calls == [blueprint.Blueprint, WorldReview]
    assert list((folder / "world/revisions").glob("*/RESUME-INPUT.json"))
    assert blueprint.seed_core(root, folder, models=models)["calls_this_invocation"] == 0
    (folder / "world/CORE-REVIEW.json").write_text("{}")
    with pytest.raises(ValueError, match="drift"):
        blueprint.load_core(root, folder)


def test_resume_cannot_silently_change_company_or_spec(fresh):
    root, folder = fresh
    models = CoreModels()
    blueprint.seed_core(root, folder, models=models)
    write(folder / "world-spec.json", {"minimum_entities": 20})
    with pytest.raises(ValueError, match="author inputs changed"):
        blueprint.seed_core(root, folder, models=models, resume=True)
    assert len(models.calls) == 2


@pytest.mark.parametrize(
    "app,collection,accepted",
    [
        ("google_calendar_mock", "events", True),
        ("hubspot_mock", "meetings", True),
        ("gmail_mock", "emails", False),
    ],
)
def test_upcoming_meetings_are_allowed_but_future_sent_mail_is_not(app, collection, accepted):
    core = CoreModels().value
    plan = core.population_plan[0].model_copy(
        update={
            "app_id": app,
            "collection": collection,
            "last_date": "2026-09-10",
        }
    )
    value = core.model_copy(update={"population_plan": [plan]})
    report = blueprint.check_blueprint(
        value,
        {
            "entities": [{"id": "a", "apps": [app]}],
            "history": [{"id": "h", "date": "2026-08-01", "apps": [app]}],
        },
        {},
        [],
        {},
        {app: {"top_level_keys": [collection]}},
        [],
        {},
    )
    assert report["ok"] is accepted


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_window",
        "duplicate_history",
        "empty_material",
        "missing_reference",
        "missing_app",
        "broken_formula",
        "bad_csv",
    ],
)
def test_invalid_core_stops_before_paying_for_review(fresh, defect):
    root, folder = fresh
    models = CoreModels()
    world = json.loads(models.value.entities_json)
    if defect == "wrong_window":
        write(folder / "world-spec.json", {"history_start": "2026-08-01", "history_months": 1})
        world["history"][0]["date"] = "2023-08-01"
    elif defect == "duplicate_history":
        world["history"].append(dict(world["history"][0]))
    elif defect == "missing_reference":
        world["history"][0]["account_id"] = "missing-account"
    elif defect == "missing_app":
        world["history"][0].pop("apps")
    else:
        first, *rest = models.value.materials
        content = {
            "empty_material": "x",
            "broken_formula": "Name,Value\nTotal,=#REF!",
            "bad_csv": "Name,Value\nIncorrect,1,2",
        }[defect]
        first = first.model_copy(
            update={"content": content, **({"path": "Documents/export.csv"} if defect == "bad_csv" else {})}
        )
        models.value = models.value.model_copy(update={"materials": [first, *rest]})
    models.value = models.value.model_copy(update={"entities_json": json.dumps(world)})
    report = blueprint.seed_core(root, folder, models=models)
    assert report["status"] == "core_needs_revision"
    assert report["checks"]["errors"]
    assert report["review"] == "not_run"
    assert models.calls == [blueprint.Blueprint]


def test_current_checks_revalidate_a_previously_accepted_checkpoint(fresh, monkeypatch):
    root, folder = fresh
    models = CoreModels()
    blueprint.seed_core(root, folder, models=models)
    marker = read(folder / "world/CORE.json")
    marker["inputs"]["implementation"] = "older-checker-version"
    write(folder / "world/CORE.json", marker)
    assert blueprint.seed_core(root, folder, models=models)["calls_this_invocation"] == 0
    monkeypatch.setattr(
        blueprint, "check_blueprint", lambda *args: {"ok": False, "errors": ["new invariant failed"]}
    )
    with pytest.raises(ValueError, match="current checks"):
        blueprint.load_core(root, folder)
    assert len(models.calls) == 2


def test_history_windows_honor_the_plan_and_do_not_create_empty_windows():
    windows = state_seed.time_windows("2026-09-01", 3, first_date="2025-03-01", last_date="2026-06-30")
    assert windows[0][0] == "2025-03-01"
    assert windows[-1][1] == "2026-06-30"
    assert state_seed.time_windows("2026-09-01", 3, first_date="2026-08-20") == []


def test_population_counts_unique_records_and_detects_missing_collections():
    target = CoreModels().value.population_plan[0].model_dump()
    target["target_records"] = 2
    assert state_seed.population_findings(
        {"demo_mock": {"tickets": [{"id": "same"}, {"id": "same"}]}}, [target]
    )
    assert state_seed.population_findings({}, [target])
    assert not state_seed.population_findings(
        {"demo_mock": {"tickets": [{"id": "one"}, {"id": "two"}]}}, [target]
    )
