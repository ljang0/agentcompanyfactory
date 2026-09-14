import pytest
from test_hub_app import StubHub
from test_hub_world import SEEDED, USERS, company  # noqa: F401

from company_envs.storage import write
from company_envs.world.hub_app import HubClient
from company_envs.world.trial_access import mask_records, proxy_factory
from company_envs.world.trial_evidence import ObservedWorld


def test_input_ablation_is_confined_to_the_named_worker_view(company):  # noqa: F811
    root, folder = company
    write(folder / "world/demo_mock.state.json", SEEDED)
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    stub = StubHub()
    world = ObservedWorld(folder, root / "hub", root / "work", host="127.0.0.1", root=root)
    try:
        world.start(
            server_factory=lambda _: type(
                "S", (), {"client": HubClient(stub.url), "stop": lambda self: None}
            )(),
            proxy_factory=proxy_factory(world, {"worker_id": "w2", "references": ["demo_mock.tickets#7"]}),
        )
        clients = {
            worker: HubClient(url.split("/?")[0])
            for worker, url in world.endpoints["demo_mock"]["workers"].items()
        }
        hidden = clients["w2"].current(world.sid)["stored_state"]
        assert hidden["tickets"] == []
        assert clients["w1"].current(world.sid)["stored_state"]["tickets"] == [{"id": 7}]
        clients["w2"].update(world.sid, {**hidden, "tickets": [{"id": 8}]})
        assert clients["w2"].current(world.sid)["stored_state"]["tickets"] == [{"id": 8}]
        snapshot = HubClient(stub.url).inspect(world.sid)
        assert snapshot["initial_state"] == SEEDED
        assert snapshot["current_state"]["tickets"] == [{"id": 7}, {"id": 8}]
        with pytest.raises(Exception):  # noqa: B017 -- HTTP denial
            clients["w2"].update(world.sid, {**hidden, "tickets": [{"id": 7, "secret": "guess"}]})
    finally:
        world.stop()
        stub.close()


def test_mask_does_not_remove_other_collections_or_mutate_input():
    initial = {"documents": {"hidden": {"id": "hidden", "content": "private"}}, "users": [{"id": "hidden"}]}
    masked = mask_records(initial, ["docs.documents#hidden"], "docs")
    assert masked["documents"] == {} and masked["users"] == initial["users"]
    assert initial["documents"]["hidden"]["content"] == "private"
