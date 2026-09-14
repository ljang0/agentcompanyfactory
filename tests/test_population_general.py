"""Cross-domain repair regressions: no pilot names, IDs, ledger columns or working hours."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from company_envs.storage import digest, read, write
from company_envs.world.population_dialogue import author_dialogue, compile_dialogue, dialogue_source


def conversation(rid, second="reviewer"):
    return {
        "id": rid,
        "messages": [
            {"sender": "technician", "text": "The inspection photo is unclear. Can you check it?"},
            {"sender": second, "text": "Yes, the replacement image shows the serial number."},
        ],
    }


def test_repair_retries_only_failed_units_and_reuses_unchanged_sources(tmp_path):
    cases = [
        {
            "id": i,
            "request": "Check an inspection photo",
            "permitted_speakers": {
                "technician": "collects inspection records",
                "reviewer": "checks documentation",
            },
        }
        for i in ["inspection-A", "inspection-B"]
    ]
    prompts = []

    def call(job, prompt, response_type):
        body = json.loads(prompt.split("\n")[-1])
        prompts.append(body)
        values = [
            conversation(c["id"], "outsider" if len(prompts) == 1 and c["id"].endswith("B") else "reviewer")
            for c in body["cases"]
        ]
        return response_type.model_validate({"dialogues": values}), {"call_id": str(len(prompts))}

    model = SimpleNamespace(call=call)
    result, _ = author_dialogue(cases, model, tmp_path)
    assert [c["id"] for c in prompts[1]["cases"]] == ["inspection-B"]
    assert prompts[1]["repair"][0]["previous"]["messages"][1]["sender"] == "outsider"
    assert author_dialogue(cases, model, tmp_path)[0] == result
    assert len(prompts) == 2
    cases[1]["request"] = "Check a revised inspection photo"
    author_dialogue(cases, model, tmp_path)
    assert len(prompts) == 3 and [c["id"] for c in prompts[-1]["cases"]] == ["inspection-B"]
    # A recomputed content hash is insufficient to bypass speaker validation.
    path = next(tmp_path.glob("units/*.json"))
    saved = read(path)
    saved["value"]["messages"][1]["sender"] = "outsider"
    saved["output_hash"] = digest(saved["value"])
    write(path, saved)
    if saved["value"]["id"] == "inspection-B":
        # Inspect the input version that owns this checkpoint.
        cases[1]["request"] = "Check an inspection photo"
    with pytest.raises(ValueError, match="checkpoint"):
        author_dialogue(cases, model, tmp_path)
    assert len(prompts) == 3


def test_invalid_authoring_inputs_fail_before_model_call(tmp_path):
    model = SimpleNamespace(call=lambda *a: pytest.fail("must not call model"))
    case = {"id": "x", "permitted_speakers": {"a": "role", "b": "role"}}
    with pytest.raises(ValueError, match="unique"):
        author_dialogue([case, case], model, tmp_path)
    with pytest.raises(ValueError, match="supplied roles"):
        author_dialogue([{**case, "permitted_speakers": ["a", "b"]}], model, tmp_path)


def test_valid_units_survive_exhausted_repair_budget(tmp_path):
    case = lambda rid: {"id": rid, "permitted_speakers": {"technician": "role", "reviewer": "role"}}
    calls = []

    def call(job, prompt, response_type):
        requested = json.loads(prompt.split("\n")[-1])["cases"]
        calls.append([c["id"] for c in requested])
        return response_type.model_validate(
            {
                "dialogues": [
                    conversation(c["id"], "outsider" if c["id"] == "bad" else "reviewer") for c in requested
                ]
            }
        ), {}

    with pytest.raises(ValueError, match="three attempts"):
        author_dialogue([case("good"), case("bad")], SimpleNamespace(call=call), tmp_path)
    assert calls == [["good", "bad"], ["bad"], ["bad"]]
    author_dialogue([case("good")], SimpleNamespace(call=lambda *a: pytest.fail("cached")), tmp_path)


def slack_fixture():
    state = {
        "channels": [
            {
                "channelId": "inspection-room",
                "members": ["technician", "reviewer"],
                "createdAt": "2026-07-04T21:00:00+09:00",
            }
        ],
        "messages": {
            "inspection-room": [
                {"messageId": "native-root-73", "threadId": None, "content": "Old question"},
                {"messageId": "native-answer-4", "threadId": "thread-6", "content": "Old answer"},
                {"messageId": "MSG-EP-unrelated", "content": "Another real conversation"},
                {"messageId": "NOTICE-ORD-unrelated", "content": "An unrelated system notice"},
            ]
        },
        "threads": {
            "thread-6": {
                "threadId": "thread-6",
                "channelId": "inspection-room",
                "parentMessageId": "native-root-73",
                "replies": ["native-answer-4"],
            }
        },
    }
    binding = {
        "id": "inspection-A",
        "thread_id": "thread-6",
        "source_hash": digest(dialogue_source(state, "thread-6")),
        "starts_at": "2026-07-04T23:56:00+09:00",
        "ends_at": "2026-07-05T00:06:00+09:00",
        "permitted_speakers": ["technician", "reviewer"],
    }
    return state, binding


def test_dialogue_projection_preserves_unrelated_history_and_uses_source_window():
    from company_envs.world.population_quality import instant

    state, binding = slack_fixture()
    original = deepcopy(state)
    output = compile_dialogue(state, [conversation("inspection-A")], [binding])
    assert state == original
    assert output["messages"]["inspection-room"][:2] == original["messages"]["inspection-room"][2:]
    messages = output["messages"]["inspection-room"][2:]
    assert [m["messageId"] for m in messages] == ["native-root-73", "native-answer-4"]
    assert (
        instant(binding["starts_at"])
        < instant(messages[0]["timestamp"])
        < instant(messages[1]["timestamp"])
        < instant(binding["ends_at"])
    )
    assert messages[0]["timestamp"].endswith("+09:00")
    with pytest.raises(ValueError, match="source changed"):
        compile_dialogue(output, [conversation("inspection-A")], [binding])
    with pytest.raises(ValueError, match="native group access"):
        compile_dialogue(state, [conversation("inspection-A", "outsider")], [binding])


def test_dialogue_does_not_pad_volume_or_drop_notification_references():
    state, binding = slack_fixture()
    state["messages"]["inspection-room"].append({"messageId": "third", "threadId": "thread-6"})
    state["threads"]["thread-6"]["replies"].append("third")
    binding["source_hash"] = digest(dialogue_source(state, "thread-6"))
    state["notifications"] = [{"messageId": "third"}]
    with pytest.raises(ValueError, match="notification"):
        compile_dialogue(state, [conversation("inspection-A")], [binding])
    state["notifications"] = []
    output = compile_dialogue(state, [conversation("inspection-A")], [binding])
    assert sum(map(len, output["messages"].values())) == sum(map(len, state["messages"].values())) - 1


def test_derived_repairs_preserve_source_facts_and_are_idempotent():
    from company_envs.world.population_derived import repair_derived_state
    from company_envs.world.population_quality import quality_errors

    state = {
        "contacts": [{"id": "contact", "companyId": "lab", "lastActivityDate": "2026-01-01"}],
        "notes": [
            {
                "id": "note",
                "associatedType": "company",
                "associatedId": "lab",
                "createDate": "2026-03-01",
                "body": "Inspection incomplete",
            }
        ],
        "deals": [{"id": "contract", "stage": "closed_won", "closedLostReason": "Contradictory source"}],
    }
    original = deepcopy(state)
    repaired, changes = repair_derived_state("hubspot_mock", state)
    assert state == original and repaired["notes"] == state["notes"] and repaired["deals"] == state["deals"]
    assert changes == [
        {
            "path": ["contacts", 0, "lastActivityDate"],
            "before": "2026-01-01",
            "after": "2026-03-01T00:00:00+00:00",
        }
    ]
    assert repair_derived_state("hubspot_mock", repaired) == (repaired, [])
    assert any("loss reason" in e for e in quality_errors({"hubspot_mock": repaired}))


def test_incomplete_snapshot_is_repairable_but_never_acceptable(tmp_path, monkeypatch):
    from company_envs.world import population_repair
    from company_envs.world import population_snapshot as module

    core = SimpleNamespace(population_plan=[])
    monkeypatch.setattr(module, "load_core", lambda *a: (core, {}))
    monkeypatch.setattr(population_repair, "load_core", lambda *a: (core, {}))
    monkeypatch.setattr(population_repair, "load_config", lambda *a: {"generation": {}, "design": {}})
    app = {"app_id": "lab_mock", "state_file": "world/lab_mock.state.json", "top_level_keys": ["records"]}
    monkeypatch.setattr(
        population_repair,
        "app_contract",
        lambda *a: ({"apps": [app, {"app_id": "missing"}]}, [app, {"app_id": "missing"}]),
    )
    state = {"records": [{"id": "x", "status": "wrong"}]}
    write(tmp_path / "world/CORE.json", {"hashes": {}})
    for file in [
        "world/world.json",
        "world/population/EFFECTIVE-WORLD.json",
        "world/identities.json",
        "world/worker_apps.json",
    ]:
        write(tmp_path / file, {})
    write(tmp_path / app["state_file"], state)
    write(tmp_path / "world/population/lab_mock.json", {"inputs": "old", "state_hash": digest(state)})
    write(
        tmp_path / "world/POPULATION.json",
        {
            "schema_version": 2,
            "status": "population_needs_repair",
            "native_errors": ["invalid status"],
            "core_hashes": {},
            "states": {"lab_mock": {"path": app["state_file"], "state_hash": digest(state)}},
            "artifacts": {},
            "effective_world": "world/population/EFFECTIVE-WORLD.json",
        },
    )
    with pytest.raises(ValueError, match="Stage 3"):
        module.population_snapshot(tmp_path, tmp_path)
    baseline = module.population_snapshot(tmp_path, tmp_path, require_complete=False)
    patch = {
        "baseline": baseline,
        "changes": [
            {
                "app": "lab_mock",
                "path": ["records", 0, "status"],
                "before": "wrong",
                "after": "pending",
                "reason": "Match the recorded inspection status",
            }
        ],
    }
    population_repair.apply_native_patch(tmp_path, tmp_path, patch)
    assert read(tmp_path / app["state_file"])["records"][0]["status"] == "pending"
    with pytest.raises(ValueError, match="Stage 3"):
        module.population_snapshot(tmp_path, tmp_path)
    with pytest.raises(ValueError, match="state drift"):
        module.population_snapshot(tmp_path, tmp_path, require_complete=False)


def test_runtime_uses_declared_zone_or_explicit_utc(tmp_path):
    from company_envs.world.runtime_acceptance import runtime_timezone

    write(tmp_path / "world/POPULATION.json", {"effective_world": "world/population/EFFECTIVE-WORLD.json"})
    write(tmp_path / "world/population/EFFECTIVE-WORLD.json", {"timezone": "Asia/Kolkata"})
    assert runtime_timezone(tmp_path) == "Asia/Kolkata"
    write(tmp_path / "world/population/EFFECTIVE-WORLD.json", {})
    assert runtime_timezone(tmp_path) == "UTC"


def test_dialogue_uses_native_dm_participants():
    state, binding = slack_fixture()
    state["dms"] = [{"dmId": "private-inspection", "participants": ["technician", "reviewer"]}]
    state["channels"] = []
    state["messages"]["private-inspection"] = state["messages"].pop("inspection-room")
    state["threads"]["thread-6"].update(channelId=None, dmId="private-inspection")
    binding["source_hash"] = digest(dialogue_source(state, "thread-6"))
    result = compile_dialogue(state, [conversation("inspection-A")], [binding])
    assert len(result["messages"]["private-inspection"]) == 4
