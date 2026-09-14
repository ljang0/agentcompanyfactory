import json

import pytest
from test_hub_app import StubHub
from test_hub_world import SEEDED, USERS, company  # noqa: F401 -- shared native protocol fixture

from company_envs.storage import write
from company_envs.world.golden import merge_patch
from company_envs.world.hub_app import HubClient
from company_envs.world.hub_mail import browser_mail, canonical_mail
from company_envs.world.staged_calibration import apply_case
from company_envs.world.trial_evidence import ObservedWorld, read_events, record_changes


def test_committed_actor_evidence_and_reset(company):  # noqa: F811
    root, folder = company
    write(folder / "world/demo_mock.state.json", SEEDED)
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    stub = StubHub()
    world = ObservedWorld(folder, root / "hub", root / "work", host="127.0.0.1", root=root)
    try:
        world.start(
            server_factory=lambda _: type(
                "S", (), {"client": HubClient(stub.url), "stop": lambda self: None}
            )()
        )
        url = world.endpoints["demo_mock"]["workers"]["w2"].split("/?")[0]
        client = HubClient(url)
        client.update(
            world.sid, {**SEEDED, "currentUser": USERS[1], "tickets": [{"id": 7, "status": "closed"}]}
        )
        events = read_events(folder, world.sid)
        assert len(events) == 1 and events[0]["worker_id"] == "w2"
        assert events[0]["changes"]["demo_mock.tickets#7"]["after"]["status"] == "closed"
        assert all("currentUser" not in ref for ref in events[0]["changes"])
        world.reset()
        assert read_events(folder, world.sid) == []
        assert HubClient(stub.url).current(world.sid)["stored_state"] == SEEDED
        archive = list((folder / "runtime/evidence/history").glob("*.jsonl"))
        assert json.loads(archive[0].read_text())["worker_id"] == "w2"
    finally:
        world.stop()
        stub.close()


def test_record_changes_preserves_deleted_record_and_ignores_interface():
    changes = record_changes(
        {"app": {"tickets": [{"id": 4, "amount": 20}], "ui": {}}},
        {"app": {"tickets": [], "ui": {"view": "home"}}},
    )
    assert changes == {"app.tickets#4": {"before": {"id": 4, "amount": 20}, "after": None}}


@pytest.mark.parametrize("path", ["", "/initial", "/credentials", "final"])
def test_fixtures_cannot_replace_baseline_or_controller_data(path):
    with pytest.raises(ValueError, match="must stay"):
        apply_case(
            {"initial": {}, "final": {}, "events": []},
            {"edits": [{"path": path, "value_json": "{}"}], "remove_worker_events": []},
        )


def test_fixtures_copy_and_preserve_original_evidence():
    reference = {"initial": {}, "final": {"records": {"a/b": 2}}, "events": [{"worker_id": "owner"}]}
    result = apply_case(
        reference,
        {"edits": [{"path": "/final/records/a~1b", "value_json": "3"}], "remove_worker_events": ["owner"]},
    )
    assert reference["final"]["records"]["a/b"] == 2 and reference["events"]
    assert result["final"]["records"]["a/b"] == 3 and not result["events"]


def test_reference_append_preserves_unseen_messages_and_thread_history():
    original = {
        "messages": {"C": [{"messageId": "older", "content": "private history"}]},
        "threads": {"T": {"replies": ["older"]}},
    }
    patch = {
        "messages": {"C": {"$append": [{"messageId": "new", "content": "reply"}]}},
        "threads": {"T": {"replies": {"$append": ["new"]}}},
    }
    final = merge_patch(original, patch)
    assert [r["messageId"] for r in final["messages"]["C"]] == ["older", "new"]
    assert final["threads"]["T"]["replies"] == ["older", "new"]
    assert merge_patch(final, patch) == final
    with pytest.raises(ValueError, match="cannot replace"):
        merge_patch(original, {"messages": {"C": {"$append": [{"messageId": "older", "content": "forged"}]}}})


def test_reference_draft_changes_survive_the_browser_mail_adapter():
    initial = {
        "emails": [{"id": "sent", "folder": "sent", "body": "past work"}],
        "drafts": [{"id": "draft", "folder": "drafts", "body": "unfinished"}],
    }
    visible = browser_mail(initial)
    proposed = merge_patch(
        canonical_mail(visible),
        {"drafts": [{"id": "draft", "folder": "drafts", "body": "useful completed reply"}]},
    )
    persisted = canonical_mail(browser_mail(proposed))
    assert persisted["emails"] == initial["emails"]
    assert persisted["drafts"][0]["body"] == "useful completed reply"
    assert initial["drafts"][0]["body"] == "unfinished"
