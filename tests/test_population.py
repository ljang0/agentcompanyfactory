import json
from copy import deepcopy

import pytest

from company_envs.storage import digest
from company_envs.world.bulk import BulkSpec
from company_envs.world.population import additive_world, check_bulk_targets, missing_targets


def test_additions_preserve_parent_and_existing_facts():
    base = {"accounts": [{"id": "a", "name": "A", "status": "active"}]}
    original = deepcopy(base)
    extension = {
        "parent_world_hash": digest(base),
        "collections": {
            "accounts": [{"id": "a", "domain": "a.example"}, {"id": "b", "name": "B"}],
            "contacts": [{"id": "c", "account_id": "a"}],
        },
    }
    world = additive_world(base, extension)
    assert base == original
    assert world["accounts"][0] == {**base["accounts"][0], "domain": "a.example"}
    assert len(world["accounts"]) == 2
    world["contacts"][0]["account_id"] = "b"
    assert extension["collections"]["contacts"][0]["account_id"] == "a"


@pytest.mark.parametrize("rows", [[{"id": "a", "status": "closed"}], [{"id": "x"}, {"id": "x"}]])
def test_additions_reject_changed_facts_and_duplicates(rows):
    base = {"accounts": [{"id": "a", "status": "active"}]}
    with pytest.raises(ValueError):
        additive_world(base, {"parent_world_hash": digest(base), "collections": {"accounts": rows}})


def test_additions_reject_other_parent():
    with pytest.raises(ValueError, match="different canonical world"):
        additive_world({}, {"parent_world_hash": "old"})


def test_duplicate_records_do_not_satisfy_population():
    plan = [{"app_id": "mail", "collection": "emails", "target_records": 3}]
    targets = missing_targets({"emails": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}, plan, "mail")
    assert targets[0]["missing_records"] == 1
    assert missing_targets({}, plan, "other") == []


def spec():
    return BulkSpec(
        app_id="mail",
        collection="emails",
        what="receipts",
        count=2,
        start="2025-03-01",
        end="2026-08-31",
        cadence="random",
        id_prefix="email",
        template_json='{"id":"{{seq}}"}',
    )


def test_bulk_requires_exact_counts_and_bounded_dates():
    targets = [
        {"collection": "emails", "missing_records": 2, "first_date": "2025-03-01", "last_date": "2026-08-31"}
    ]
    assert not check_bulk_targets([spec()], targets)
    assert check_bulk_targets([], targets)
    assert check_bulk_targets([spec(), spec()], targets)
    assert check_bulk_targets([spec().model_copy(update={"end": "2026-09-01"})], targets)
    assert check_bulk_targets([spec().model_copy(update={"collection": "drafts"})], targets)


def test_small_collections_can_share_a_bounded_authoring_call():
    from company_envs.world.state_seed import collection_groups

    keys = ["campaigns", "ads", "reports", "settings", "emails"]
    fields = {k: [] for k in keys if k != "settings"}
    groups = collection_groups(keys, fields, 100, {"campaigns", "ads", "reports"})
    assert ["emails"] in groups
    assert ["campaigns", "ads", "reports", "settings"] in groups


def test_native_map_ids_count_as_records():
    plan = [{"app_id": "docs", "collection": "documents", "target_records": 3}]
    assert (
        missing_targets({"documents": {"d1": {"title": "One"}, "d2": {"title": "Two"}}}, plan, "docs")[0][
            "missing_records"
        ]
        == 1
    )


def test_single_user_bulk_identity_and_explicit_worker_roster():
    from company_envs.world.bulk import expand, people_of
    from company_envs.world.bulk_layer import check_addressing_template

    state = {"user": {"id": "mara", "username": "Mara", "email": "mara@example.test"}}
    people = people_of(state)
    template = {"to": [{"email": "{{person.email}}"}], "body": "Hi {{person}}"}
    item = spec().model_copy(update={"template_json": json.dumps(template)})
    assert expand(item, people)[0]["to"] == [{"email": "mara@example.test"}]
    assert not check_addressing_template(item, {"to": [{"email": "mara@example.test"}]}, state)
    other = [{"id": "imani", "name": "Imani", "email": "imani@example.test"}]
    assert not check_addressing_template(item, {"to": [{"email": "imani@example.test"}]}, state, people=other)


def test_recurring_calendar_title_is_not_a_prose_defect():
    from company_envs.world.world_check import check_texture

    events = [
        {
            "id": str(i),
            "title": "Weekly reseller desk check in",
            "description": "",
        }
        for i in range(30)
    ]
    assert not check_texture("google_calendar_mock", {"events": events})
    for event in events:
        event["description"] = "The same vague meeting description repeated everywhere."
    assert check_texture("google_calendar_mock", {"events": events})


def test_spreadsheet_codes_are_not_scored_as_prose():
    from company_envs.world.world_check import prose_texts

    assert prose_texts(
        {}, {"orders.csv": "Order,Product\nORD-123,CB100", "notes.txt": "Follow up with central orders."}
    ) == {"materials:notes.txt": "Follow up with central orders."}


def test_invoice_contents_are_not_drive_metadata_restatement():
    from company_envs.world.world_check import check_machine_tells

    items = [
        {
            "id": str(i),
            "name": f"Invoice {i}.pdf",
            "size": 1000,
            "content": "Size: L\nQuantity: 24\nLine amount: 96.00",
        }
        for i in range(30)
    ]
    assert not check_machine_tells("google_drive_mock", {"items": items})
    for item in items:
        item["description"] = "Owner: Imani Brooks"
    assert check_machine_tells("google_drive_mock", {"items": items})


@pytest.mark.parametrize("partial_resume", [False, True])
def test_app_exception_preserves_independent_outputs(tmp_path, monkeypatch, partial_resume):
    from types import SimpleNamespace

    from company_envs.storage import read, write
    from company_envs.world import population

    folder = tmp_path / "company"
    write(folder / "world/CORE.json", {"hashes": {}})
    apps = [{"app_id": name, "top_level_keys": ["emails"], "schema_document": ""} for name in ["bad", "good"]]
    plan = [
        {"app_id": name, "collection": "emails", "target_records": 1, "record_unit": "emails"}
        for name in ["bad", "good"]
    ]
    core = SimpleNamespace(
        reference_date="2026-09-01",
        operating_scope="desk",
        population_plan=[SimpleNamespace(model_dump=lambda p=p: p) for p in plan],
    )
    monkeypatch.setattr(population, "load_config", lambda root: {"design": {}, "generation": {}})
    monkeypatch.setattr(population, "load_core", lambda *args: (core, {}))
    monkeypatch.setattr(population, "app_contract", lambda *args: ({"apps": apps, "workers": []}, apps))
    monkeypatch.setattr(population, "validate_core", lambda *args: ({}, {}, [], {}))
    monkeypatch.setattr(population, "load_skill", lambda *args: ("instructions", {}))
    monkeypatch.setattr(population, "core_payload", lambda *args: {"company": {}})
    monkeypatch.setattr(population, "check_folder", lambda *args, **kwargs: {"ok": True})

    def author(app):
        if app["app_id"] == "bad":
            raise AttributeError("native adapter failed")
        return app["app_id"], {"emails": [{"id": "e1"}]}

    monkeypatch.setattr(population, "make_author", lambda *args, **kwargs: author)
    result = population.populate(tmp_path, folder, models=object())
    assert result["status"] == "population_needs_repair"
    assert result["failures"] == {"bad": "AttributeError: native adapter failed"}
    assert read(folder / "world/good.state.json") == {"emails": [{"id": "e1"}]}
    assert (folder / "world/population/good.json").is_file()
    assert read(folder / "world/population/RUN.json")["status"] == "population_needs_repair"
    if partial_resume:
        checkpoint = read(folder / "world/population/good.json")
        # Simulate interruption after a successfully saved literal batch but before app commit.
        (folder / "world/population/good.json").unlink()
        write(folder / "world/population/good.partial.json", {"emails": [{"id": "e1"}]})
        write(
            folder / "world/population/good.partial-meta.json",
            {
                "inputs": checkpoint["inputs"],
                "initial_state_hash": None,
                "state_hash": digest({"emails": [{"id": "e1"}]}),
            },
        )
        monkeypatch.setattr(
            population,
            "make_author",
            lambda *args, **kwargs: lambda app: pytest.fail("partial was reauthored"),
        )
        resumed = population.populate(tmp_path, folder, models=object(), app_ids=["good"])
        assert "good" not in resumed["failures"]
        assert not (folder / "world/population/good.partial-meta.json").exists()
        return
    write(folder / "world/population/good.initial.json", {"emails": [{"id": "changed"}]})
    resumed = population.populate(tmp_path, folder, models=object(), app_ids=["good"])
    assert "supplied initial population changed" in resumed["failures"]["good"]
    assert read(folder / "world/good.state.json") == {"emails": [{"id": "e1"}]}
    assert "good" not in resumed["shortfalls"]
    assert read(folder / "world/POPULATION.json")["states"]["good"]["state_hash"] == digest(
        {"emails": [{"id": "e1"}]}
    )


def test_slack_notification_reference_does_not_own_message_ids():
    from company_envs.world.world_check import check_containment

    state = {
        "notifications": [{"notificationId": "n", "messageId": "parent"}],
        "messages": {"ch": [{"messageId": "parent", "threadId": "t"}]},
        "threads": {
            "t": {
                "threadId": "t",
                "parentMessageId": "parent",
                "replies": [{"messageId": "reply", "threadId": "t", "content": "Done."}],
            }
        },
    }
    found = check_containment("slack_mock", state)
    assert any("not in messages" in f["message"] for f in found)
    assert not any("not in notifications" in f["message"] for f in found)
    assert any("thread parents" in f["message"] for f in found)
    state["messages"]["ch"][0]["threadId"] = None
    state["messages"]["ch"].append(state["threads"]["t"]["replies"][0])
    assert check_containment("slack_mock", state) == []


def test_folder_check_reads_staged_world_date_and_bulk_provenance(tmp_path, monkeypatch):
    from company_envs.storage import write
    from company_envs.world import world_check

    write(tmp_path / "apps.json", {"apps": [{"app_id": "mail"}]})
    write(tmp_path / "company.json", {"workers": []})
    write(tmp_path / "world/world.json", {"accounts": []})
    write(tmp_path / "world/CORE.json", {"core_metadata": {"reference_date": "2026-09-01"}})
    write(
        tmp_path / "world/population/EXTENSION.json",
        {
            "parent_world_hash": digest({"accounts": []}),
            "collections": {"accounts": [{"id": "a"}]},
        },
    )
    write(tmp_path / "world/population/mail.json", {"bulk_ids": ["e"]})
    observed = {}

    def check(world, states, identities, workers, date, **kwargs):
        observed.update(world=world, date=date, **kwargs)
        return {"ok": True, "findings": []}

    monkeypatch.setattr(world_check, "check_world", check)
    world_check.check_folder(tmp_path, tmp_path, {"mail": {}}, identities={}, materials={}, worker_apps={})
    assert observed["world"] == {"accounts": [{"id": "a"}]}
    assert observed["date"] == "2026-09-01"
    assert observed["bulk_ids"] == {"mail": {"e"}}


def test_normal_population_repairs_derived_metadata_and_resumes_without_models(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from company_envs.storage import read, write
    from company_envs.world import population

    state = {
        "contacts": [{"id": "person", "companyId": "lab", "lastActivityDate": "2026-01-01"}],
        "notes": [{"id": "inspection", "companyId": "lab", "createDate": "2026-04-01"}],
    }
    app = {"app_id": "hubspot_mock", "top_level_keys": list(state), "schema_document": ""}
    core = SimpleNamespace(reference_date="2026-09-01", operating_scope="laboratory", population_plan=[])
    write(tmp_path / "world/CORE.json", {"hashes": {}})
    monkeypatch.setattr(population, "load_core", lambda *a: (core, {}))
    monkeypatch.setattr(population, "load_config", lambda *a: {"design": {}, "generation": {}})
    monkeypatch.setattr(population, "app_contract", lambda *a: ({"apps": [app], "workers": []}, [app]))
    monkeypatch.setattr(population, "validate_core", lambda *a: ({}, {}, [], {}))
    monkeypatch.setattr(population, "load_skill", lambda *a: ("", {}))
    monkeypatch.setattr(population, "core_payload", lambda *a: {"company": {}})
    monkeypatch.setattr(population, "check_folder", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr(
        population, "make_author", lambda *a, **kw: lambda app: (app["app_id"], deepcopy(state))
    )
    result = population.populate(tmp_path, tmp_path, models=object())
    assert result["status"] == "populated_for_review" and not result["native_errors"]
    repairs = list((tmp_path / "world/population/derived-repairs").glob("*.json"))
    assert len(repairs) == 1
    assert read(repairs[0])["changes"][0]["before"] == "2026-01-01"
    saved = read(tmp_path / "world/hubspot_mock.state.json")
    assert saved["notes"] == state["notes"]
    monkeypatch.setattr(
        population, "make_author", lambda *a, **kw: lambda app: pytest.fail("saved app reauthored")
    )
    assert population.populate(tmp_path, tmp_path, models=object())["status"] == "populated_for_review"
    assert read(tmp_path / "world/hubspot_mock.state.json") == saved
    assert list((tmp_path / "world/population/derived-repairs").glob("*.json")) == repairs
