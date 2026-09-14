"""Literal continuation preserves anchors and progress across interrupted calls."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from company_envs.world.population_records import author_records, missing_targets

TARGET = {
    "app_id": "calendar",
    "collection": "events",
    "target_records": 3,
    "record_unit": "separate appointments",
    "first_date": "2025-09-01",
    "last_date": "2026-09-01",
}


class Replies:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def call(self, job, prompt, response_type):
        self.requests.append(json.loads(prompt[prompt.rfind("\n{") + 1 :]))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(state_json=json.dumps({"events": reply})), {"call": len(self.requests)}


def test_interruption_keeps_new_records_and_resume_never_replaces_anchors():
    state = {"events": [{"id": "old", "title": "Accepted appointment"}]}
    saved = []
    first = Replies(
        [{"id": "old", "title": "Unwanted replacement"}, {"id": "new-1", "title": "First review"}],
        RuntimeError("provider interrupted"),
    )
    kwargs = {"instructions": "Native app content", "save_partial": lambda: saved.append(deepcopy(state))}
    with pytest.raises(RuntimeError, match="provider interrupted"):
        author_records(state, TARGET, {}, models=first, **kwargs)
    assert state == saved[-1]
    assert state["events"] == [
        {"id": "old", "title": "Accepted appointment"},
        {"id": "new-1", "title": "First review"},
    ]
    resumed = Replies([{"id": "new-2", "title": "Second review"}])
    author_records(state, TARGET, {}, models=resumed, **kwargs)
    assert resumed.requests[0]["population_batch"]["count"] == 1
    assert resumed.requests[0]["population_batch"]["existing_ids"] == ["old", "new-1"]
    assert missing_targets(state, [TARGET], "calendar") == []


def test_repeated_existing_records_exhaust_the_bound_without_satisfying_the_target():
    state = {"events": [{"id": "old"}]}
    model = Replies(*[[{"id": "old"}]] * 3)
    author_records(state, TARGET, {}, models=model, instructions="Native content", save_partial=lambda: None)
    assert state == {"events": [{"id": "old"}]}
    assert missing_targets(state, [TARGET], "calendar")[0]["missing_records"] == 2
    assert len(model.requests) == 3


def test_completed_collection_never_calls_the_author():
    state = {"events": [{"id": str(i)} for i in range(3)]}
    model = Replies()
    assert (
        author_records(
            state, TARGET, {}, models=model, instructions="Native content", save_partial=lambda: None
        )
        == []
    )
    assert model.requests == []


@pytest.mark.parametrize("invalid_templates", [False, True])
def test_population_continues_when_templates_fail_and_reuses_the_result(
    tmp_path, monkeypatch, invalid_templates
):
    from company_envs.storage import read, write
    from company_envs.world import population
    from company_envs.world.bulk import BulkSpec, BulkSpecs
    from company_envs.world.seed_calls import AppStateResult

    original = {"events": [{"id": "old", "title": "Accepted appointment"}]}
    app = {"app_id": "calendar", "top_level_keys": ["events"], "schema_document": ""}
    core = SimpleNamespace(
        reference_date="2026-09-01",
        operating_scope="desk",
        population_plan=[SimpleNamespace(model_dump=lambda: TARGET)],
    )
    write(tmp_path / "world/CORE.json", {"hashes": {}})
    for name, replacement in {
        "load_config": lambda *a: {"design": {}, "generation": {}},
        "load_core": lambda *a: (core, {}),
        "app_contract": lambda *a: ({"apps": [app], "workers": []}, [app]),
        "validate_core": lambda *a: ({}, {}, [], {}),
        "load_skill": lambda *a: ("", {}),
        "core_payload": lambda *a: {"company": {}},
        "check_folder": lambda *a, **kw: {"ok": True},
        "make_author": lambda *a, **kw: lambda app: (app["app_id"], deepcopy(original)),
    }.items():
        monkeypatch.setattr(population, name, replacement)

    class Model:
        def __init__(self):
            self.calls = []

        def call(self, job, prompt, response_type):
            self.calls.append(response_type)
            if response_type is BulkSpecs:
                specs = (
                    [
                        BulkSpec(
                            app_id="calendar",
                            collection="events",
                            what="Appointments",
                            count=1,
                            start=TARGET["first_date"],
                            end=TARGET["last_date"],
                            id_prefix="invalid",
                            template_json='{"id":"{{seq}}"}',
                        )
                    ]
                    if invalid_templates
                    else []
                )
                return BulkSpecs(specs=specs, rationale="A proposed template plan"), {
                    "call": "template-attempt"
                }
            assert response_type is AppStateResult
            return AppStateResult(
                app_id="calendar",
                state_json=json.dumps({"events": [{"id": "new-1"}, {"id": "new-2"}]}),
                rationale="Separate appointments grounded in history",
            ), {"call": "literal-records"}

    model = Model()
    result = population.populate(tmp_path, tmp_path, models=model)
    assert result["status"] == "populated_for_review"
    attempts = 3 if invalid_templates else 1
    assert model.calls == [BulkSpecs] * attempts + [AppStateResult]
    saved = read(tmp_path / "world/calendar.state.json")
    assert saved["events"][0] == original["events"][0]
    checkpoint = read(tmp_path / "world/population/calendar.json")
    assert checkpoint["receipts"] == [{"call": "template-attempt"}] * attempts + [{"call": "literal-records"}]
    assert checkpoint["bulk_ids"] == [] and checkpoint["specs"] == []
    monkeypatch.setattr(
        population, "make_author", lambda *a, **kw: lambda app: pytest.fail("saved anchors reauthored")
    )
    assert population.populate(tmp_path, tmp_path, models=object())["status"] == "populated_for_review"
    assert read(tmp_path / "world/calendar.state.json") == saved
