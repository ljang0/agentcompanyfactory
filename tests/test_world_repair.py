"""world-repair: a checked world's errors go back to its authors, the bulk is laid again, no model runs."""

import json

from test_bulk import EMAIL, FULL_TEMPLATE, STUB_TEMPLATE, _seeded_company, spec

from company_envs.storage import read, write
from company_envs.world.bulk_layer import lay_bulk
from company_envs.world.seed_calls import AppStateResult, WorldRecordsRepair
from company_envs.world.world_repair import record_at, repair_world, strip_bulk

FAULTY = {**EMAIL, "id": "e2", "body": "expand these literally for i=1..600 to fill the inbox"}
FIXED = {**FAULTY, "body": "Hello Jo, the July invoice is attached. Ana"}


class Models:
    """Answers app_state repairs and world_records_repair calls; records every payload."""

    def __init__(self, repaired=None, world_fix=None, app_id="gmail_mock"):
        self.repaired, self.world_fix, self.app_id, self.payloads = repaired, world_fix, app_id, []

    def call(self, job, prompt, response_type):
        assert job == "world_states"
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.payloads.append(payload)
        if response_type is WorldRecordsRepair:
            assert payload["call"] == "world_records_repair" and payload["feedback"]
            fixed = {p: self.world_fix(r) for p, r in payload["records"].items()}
            return WorldRecordsRepair(rationale="fixed", records_json=json.dumps(fixed)), {
                "model": "fake",
                "call_id": "world",
            }
        assert response_type is AppStateResult and payload["call"] == "app_state"
        previous = json.loads(payload["previous_state_json"])
        fixed = self.repaired(previous) if self.repaired else previous
        return AppStateResult(app_id=self.app_id, rationale="repaired", state_json=json.dumps(fixed)), {
            "model": "fake",
            "call_id": "repair",
        }


def _layered(tmp_path, emails, template=FULL_TEMPLATE):
    """A seeded company after add-bulk: bulk records in the state file, specs and ids in BULK.json."""
    root, folder = _seeded_company(tmp_path, emails)
    bulk_spec = spec(count=12, template=template)
    state = read(folder / "world/gmail_mock.state.json")
    added, ids, _ = lay_bulk(state, [bulk_spec], 3, 400_000)
    write(folder / "world/gmail_mock.state.json", state)
    write(
        folder / "world/BULK.json",
        {"apps": {"gmail_mock": {"specs": [bulk_spec.model_dump()], "added": added, "ids": sorted(ids)}}},
    )
    return root, folder


def test_strip_bulk_removes_named_records_from_every_shape():
    state = {
        "emails": [{"id": "e1"}, {"id": "b1"}],
        "messages": {"general": [{"id": "m1"}, {"messageId": "b2"}], "ops": []},
        "documents": {"d1": {"id": "d1"}, "b3": {"id": "b3"}},
        "settings": {"theme": "dark"},
    }
    human = strip_bulk(state, {"b1", "b2", "b3"})
    assert human == {
        "emails": [{"id": "e1"}],
        "messages": {"general": [{"id": "m1"}], "ops": []},
        "documents": {"d1": {"id": "d1"}},
        "settings": {"theme": "dark"},
    }
    assert strip_bulk(state, set()) == state and strip_bulk(state, set()) is not state


def test_record_at_finds_the_record_a_pointer_lies_in():
    world = {"policies": [{"id": "P1", "content": "x"}], "company": {"name": "Acme"}}
    assert record_at(world, "/policies/0/content") == ("/policies/0", world["policies"][0])
    assert record_at(world, "/policies/0") == ("/policies/0", world["policies"][0])
    assert record_at(world, "/company/name") == ("/company", world["company"])
    assert record_at(world, "/policies") == (None, None)  # a whole collection is not a record
    assert record_at(world, "/missing/3") == (None, None)


def test_a_human_layer_fault_goes_to_the_author_and_the_bulk_is_laid_again(tmp_path):
    root, folder = _layered(tmp_path, [EMAIL, FAULTY])
    assert read(folder / "world/gmail_mock.state.json")["emails"][-1]["id"] == "stmt-12"

    def repaired(previous):
        # The author sees the human layer only, never the generated records.
        assert [e["id"] for e in previous["emails"]] == ["e1", "e2"]
        return {**previous, "emails": [EMAIL, FIXED]}

    models = Models(repaired=repaired)
    report = repair_world(root, folder, models=models)
    assert report["ok"] and report["rounds"] == 1 and report["unrepaired"] == []
    assert [p["call"] for p in models.payloads] == ["app_state"]
    feedback = models.payloads[0]["revision_feedback"]
    assert feedback[0]["target"] == "gmail_mock" and feedback[0]["evidence"] == "/emails/1/body"
    state = read(folder / "world/gmail_mock.state.json")
    assert [e["id"] for e in state["emails"]] == ["e1", "e2", *(f"stmt-{i}" for i in range(1, 13))]
    assert "expand these" not in json.dumps(state)
    assert read(folder / "world/CHECKS.json")["ok"] is True
    repair = read(folder / "world/REPAIR.json")
    assert repair["rounds"] == 1 and repair["before"]["errors"] == 1 and repair["after"]["ok"] is True
    assert repair["receipts"]["apps"]["gmail_mock"][0]["call_id"] == "repair"
    assert len(read(folder / "world/BULK.json")["apps"]["gmail_mock"]["ids"]) == 12
    assert "world/REPAIR.json" in read(folder / "MANIFEST.json")["hashes"]


def test_a_world_record_fault_is_rewritten_in_place_with_its_id_kept(tmp_path):
    root, folder = _seeded_company(tmp_path, [EMAIL])
    world = read(folder / "world/world.json")
    world["policies"] = [{"id": "P1", "title": "Refunds", "content": "Synthetic policy text about refunds."}]
    write(folder / "world/world.json", world)
    before = (folder / "world/gmail_mock.state.json").stat().st_mtime_ns

    def world_fix(record):
        return {
            **record,
            "id": "changed",
            "content": "Refunds are approved by the desk lead within ten days.",
        }

    models = Models(world_fix=world_fix)
    report = repair_world(root, folder, models=models)
    assert report["ok"] and report["rounds"] == 1 and report["world_records"] == ["/policies/0"]
    assert [p["call"] for p in models.payloads] == ["world_records_repair"]
    assert models.payloads[0]["feedback"][0]["path"] == "/policies/0/content"
    policy = read(folder / "world/world.json")["policies"][0]
    assert policy["id"] == "P1" and "Synthetic" not in policy["content"] and policy["title"] == "Refunds"
    assert read(folder / "world/CHECKS.json")["ok"] is True
    repair = read(folder / "world/REPAIR.json")
    assert repair["receipts"]["world"][0]["call_id"] == "world" and repair["receipts"]["apps"] == {}
    # An untouched app state is not rewritten (its mtime is a render input).
    assert (folder / "world/gmail_mock.state.json").stat().st_mtime_ns == before


def test_bulk_only_faults_and_exhausted_rounds_are_named_not_retried_forever(tmp_path):
    # The stub bodies fail only with the bulk records: the human layer is clean, so no author
    # round is spent and REPAIR.json says whose fault it is.
    root, folder = _layered(tmp_path, [EMAIL], template=STUB_TEMPLATE)
    models = Models()
    report = repair_world(root, folder, models=models)
    assert not report["ok"] and report["rounds"] == 0 and models.payloads == []
    assert report["unrepaired"] == ["gmail_mock /emails: appears only with the bulk layer"]
    assert read(folder / "world/CHECKS.json")["ok"] is False
    assert read(folder / "world/REPAIR.json")["after"]["errors"] == 1

    # An author that never fixes the fault gets exactly the bounded rounds.
    root, folder = _layered(tmp_path / "again", [EMAIL, FAULTY])
    models = Models(repaired=lambda previous: previous)
    report = repair_world(root, folder, rounds=2, models=models)
    assert not report["ok"] and report["rounds"] == 2
    assert [p["call"] for p in models.payloads] == ["app_state", "app_state"]
    assert report["unrepaired"] == ["gmail_mock /emails/1/body: not repaired in 2 round(s)"]
    assert len(read(folder / "world/gmail_mock.state.json")["emails"]) == 14  # layered state kept


def test_a_failed_author_call_is_recorded_and_the_disk_states_are_checked(tmp_path):
    root, folder = _layered(tmp_path, [EMAIL, FAULTY])
    report = repair_world(root, folder, models=Models(app_id="other_mock"))
    assert not report["ok"] and "returned other_mock" in report["error"]
    assert "returned other_mock" in read(folder / "world/REPAIR.json")["error"]
    assert read(folder / "world/CHECKS.json")["ok"] is False


def test_a_clean_world_only_gets_its_checks_written(tmp_path):
    root, folder = _layered(tmp_path, [EMAIL])
    models = Models()
    report = repair_world(root, folder, models=models)
    assert report == {"ok": True, "errors": 0, "rounds": 0, "world_records": [], "unrepaired": []}
    assert models.payloads == [] and read(folder / "world/CHECKS.json")["ok"] is True
    assert not (folder / "world/REPAIR.json").exists()
