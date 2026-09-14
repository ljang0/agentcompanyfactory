"""Every stage checks a company folder the same way: all app states from apps.json, bulk excluded."""

import json

import pytest

from company_envs.storage import write
from company_envs.world.bulk_layer import bulk_ids_for
from company_envs.world.world_check import check_folder


def _company(tmp_path, emails):
    folder = tmp_path / "companies" / "acme"
    write(
        folder / "apps.json",
        {"apps": [{"app_id": "gmail_mock", "state_file": "world/gmail_mock.state.json"}]},
    )
    write(folder / "company.json", {"id": "acme", "workers": []})
    write(folder / "world" / "world.json", {"company": {"name": "Acme"}})
    write(folder / "world" / "identities.json", {})
    write(folder / "world" / "SEED.json", {"reference_date": "2026-09-08"})
    write(folder / "world" / "gmail_mock.state.json", {"emails": emails})
    (tmp_path / "config.toml").write_text("[generation]\nseed = 0\n")
    return folder


def _templated(n, prefix):
    return [
        {
            "id": f"{prefix}{i:04d}",
            "from": {"email": "ops@acme.test"},
            "to": [{"email": "team@acme.test"}],
            "subject": "Weekly review preparation",
            "body": f"Weekly review preparation please for week {i}. Bring the numbers, the open items from last week, "
            "the vendor invoices that arrived since Monday, and any change in headcount for the cost centre. "
            "We will go through the variance lines first and then the forecast.",
        }
        for i in range(n)
    ]


def test_state_files_come_from_apps_json_and_missing_ones_fail(tmp_path):
    folder = _company(tmp_path, _templated(25, "e-"))
    result = check_folder(tmp_path, folder)
    assert result["apps_checked"] == ["gmail_mock"]
    assert any(f["severity"] == "error" and f["source"] == "gmail_mock" for f in result["findings"])
    (folder / "world" / "gmail_mock.state.json").unlink()
    with pytest.raises(FileNotFoundError, match="gmail_mock"):
        check_folder(tmp_path, folder)


def test_bulk_layer_records_are_not_judged_for_texture(tmp_path):
    folder = _company(tmp_path, _templated(25, "bulk-e-"))
    write(
        folder / "world" / "BULK.json",
        {"apps": {"gmail_mock": {"ids": [f"bulk-e-{i:04d}" for i in range(25)], "specs": []}}},
    )
    assert bulk_ids_for(tmp_path, folder) == {"gmail_mock": {f"bulk-e-{i:04d}" for i in range(25)}}
    result = check_folder(tmp_path, folder)
    assert not [f for f in result["findings"] if f["severity"] == "error" and f["source"] == "gmail_mock"]


def test_older_bulk_markers_reexpand_their_specs(tmp_path):
    folder = _company(tmp_path, [])
    spec = {
        "app_id": "gmail_mock",
        "collection": "emails",
        "count": 3,
        "id_prefix": "bulk-x-",
        "what": "automated reminders",
        "start": "2026-01-05",
        "end": "2026-01-26",  # three weekly instants: a cadence only holds what its window holds
        "cadence": "weekly",
        "template_json": json.dumps({"id": "{{id:bulk-x-}}", "subject": "Reminder {{seq}}"}),
        "tables_json": "{}",
    }
    write(folder / "world" / "BULK.json", {"apps": {"gmail_mock": {"specs": [spec]}}})
    ids = bulk_ids_for(tmp_path, folder)["gmail_mock"]
    assert len(ids) == 3 and all(i.startswith("bulk-x-") for i in ids)


def test_findings_that_name_another_app_repair_both_and_materials_get_rewritten():
    from company_envs.world.state_seed import MaterialsOnly, WorkerMaterial, app_mentions, repair_materials

    apps = {"google_docs_mock": {}, "google_drive_mock": {}, "asana_mock": {}, "slack_mock": {}}
    assert app_mentions(apps, "Tomasz has editor access in Docs but commenter access in Drive") == {
        "google_docs_mock",
        "google_drive_mock",
    }
    assert app_mentions(apps, "the register is skeletal") == set()

    materials = [
        WorkerMaterial(worker_id="w1", path="notes/onboarding.md", content="old"),
        WorkerMaterial(worker_id="w2", path="exports/ledger.csv", content="a,b"),
    ]

    class Model:
        def call(self, job, prompt, response_type, **_):
            assert job == "world_states" and "materials_repair" in prompt
            return MaterialsOnly(
                materials=[
                    WorkerMaterial(worker_id="w1", path="notes/onboarding.md", content="rewritten"),
                    WorkerMaterial(worker_id="w2", path="exports/ledger.csv", content="a,b"),
                    WorkerMaterial(worker_id="w3", path="stray.txt", content="not asked for"),
                ]
            ), {}

    fixed = repair_materials(
        materials, [{"target": "materials", "issue": "x"}], {"company": {}}, Model(), "skill"
    )
    assert [(m.worker_id, m.path, m.content) for m in fixed] == [
        ("w1", "notes/onboarding.md", "rewritten"),
        ("w2", "exports/ledger.csv", "a,b"),
    ]


def test_in_memory_overrides_check_exactly_what_the_files_would(tmp_path):
    """Seeding checks the world before writing it; the result must be the check stage's."""
    from company_envs.storage import read

    folder = _company(tmp_path, _templated(25, "e-"))
    (folder / "world" / "materials" / "w1" / "notes").mkdir(parents=True)
    (folder / "world" / "materials" / "w1" / "notes" / "desk.md").write_text("Desk policy v3.")
    on_disk = check_folder(tmp_path, folder)
    world = read(folder / "world" / "world.json")
    states = {"gmail_mock": read(folder / "world" / "gmail_mock.state.json")}
    for name in ("world.json", "identities.json", "SEED.json", "gmail_mock.state.json"):
        (folder / "world" / name).unlink()
    in_memory = check_folder(
        tmp_path,
        folder,
        states,
        world=world,
        identities={},
        materials={"w1/notes/desk.md": "Desk policy v3."},
        reference_date="2026-09-08",
    )
    assert in_memory == on_disk and in_memory["errors"] == 1
    with pytest.raises(FileNotFoundError):
        check_folder(tmp_path, folder, world=world)  # the app list still comes from the folder
