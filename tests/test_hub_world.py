"""Company folder -> seeded hub apps behind per-worker identity proxies, with protocol doubles (no node)."""

import json
import urllib.request

import pytest
from test_hub_app import APP, SCHEMA, StubHub

from company_envs.storage import read, write
from company_envs.world import hub_world
from company_envs.world.hub_app import HubClient

USERS = [
    {"id": 1, "name": "Dana Ortiz", "email": "dana.ortiz@acme.test", "role": "agent"},
    {"id": 2, "name": "Luis Prado", "email": "luis.prado@acme.test", "role": "agent"},
]
SEEDED = {"currentUser": USERS[0], "tickets": [{"id": 7}], "ui": {"activeView": 1}, "users": USERS}
SCHEMA_WITH_USERS = SCHEMA.replace(
    "| `ui` | object | UI state |", "| `ui` | object | UI state |\n| `users` | array | Users |"
)


@pytest.fixture
def company(tmp_path):
    root = tmp_path
    (root / "catalogs" / "app_schemas").mkdir(parents=True)
    (root / "catalogs" / "app_schemas" / "demo_mock.md").write_text(SCHEMA_WITH_USERS)
    folder = root / "companies" / "acme"
    (folder / "world").mkdir(parents=True)
    (folder / "runtime").mkdir()
    write(folder / "MANIFEST.json", {"company_id": "acme"})
    write(
        folder / "apps.json",
        {
            "identity": "per_worker_proxy",
            "workers": ["w1", "w2"],
            "identities_file": "world/identities.json",
            "apps": [
                {
                    "app_id": APP["id"],
                    "hub_seedable": True,
                    "schema": "catalogs/app_schemas/demo_mock.md",
                    "top_level_keys": ["currentUser", "tickets", "ui", "users"],
                    "identity_key": "currentUser",
                    "state_file": "world/demo_mock.state.json",
                }
            ],
        },
    )
    return root, folder


def test_incomplete_world_fails_before_any_server(company):
    root, folder = company
    with pytest.raises(ValueError, match="missing world/demo_mock.state.json"):
        hub_world.load_world(folder, root)
    write(folder / "world" / "demo_mock.state.json", {"tickets": []})
    with pytest.raises(ValueError, match="missing documented keys"):
        hub_world.load_world(folder, root)
    write(folder / "world" / "demo_mock.state.json", SEEDED)
    with pytest.raises(ValueError, match="no identity for worker w1"):
        hub_world.load_world(folder, root)
    write(
        folder / "world" / "identities.json", {"w1": {"demo_mock": {"id": 99}}, "w2": {"demo_mock": USERS[1]}}
    )
    with pytest.raises(ValueError, match="identity for w1 not in a state collection"):
        hub_world.load_world(folder, root)


def test_serve_gives_each_worker_its_own_logged_in_view_of_shared_data(company):
    root, folder = company
    write(folder / "world" / "demo_mock.state.json", SEEDED)
    write(
        folder / "world" / "identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}}
    )
    stub = StubHub()
    try:
        world = hub_world.CompanyWorld(folder, root / "hub", root / "work", host="127.0.0.1", root=root)
        world.start(
            server_factory=lambda app_id: type(
                "S", (), {"client": HubClient(stub.url), "stop": lambda self: None}
            )(),
        )
        try:
            sessions = read(folder / "runtime" / "sessions.json")
            endpoints = read(folder / "runtime" / "endpoints.json")["apps"]["demo_mock"]
            assert sessions["sid"] == "acme-ep1" and sessions["workers"] == ["w1", "w2"]
            assert set(endpoints["workers"]) == {"w1", "w2"}
            w1 = HubClient(endpoints["workers"]["w1"].split("/?")[0])
            w2 = HubClient(endpoints["workers"]["w2"].split("/?")[0])
            # Same tickets, different logged-in user.
            assert w1.current("acme-ep1")["stored_state"]["currentUser"] == USERS[0]
            assert w2.current("acme-ep1")["stored_state"]["currentUser"] == USERS[1]
            assert w2.current("acme-ep1")["stored_state"]["tickets"] == SEEDED["tickets"]
            for worker in (w1, w2):
                with pytest.raises(Exception):  # noqa: B017 - any denial is the point
                    worker.inspect("acme-ep1")
            # w2 saves as itself; the shared store keeps the canonical user and records w2's change.
            w2.update("acme-ep1", {**SEEDED, "currentUser": USERS[1], "tickets": []})
            shared = HubClient(stub.url).inspect("acme-ep1")["current_state"]
            assert shared["tickets"] == [] and shared["currentUser"] == USERS[0]
            log = (folder / "runtime" / "attribution" / "demo_mock.jsonl").read_text().splitlines()
            assert json.loads(log[-1])["worker_id"] == "w2" and json.loads(log[-1])["changed_keys"] == [
                "tickets"
            ]
            # The HTML a worker loads carries the identity bootstrap.
            page = urllib.request.urlopen(endpoints["workers"]["w1"], timeout=5).read()
            assert b'data-company-envs="identity"' in page and b'"id": "w1"' in page
            world.reset()
            assert HubClient(stub.url).inspect("acme-ep1")["current_state"] == SEEDED
            assert not (folder / "runtime/attribution/demo_mock.jsonl").read_text()
            archived = list((folder / "runtime/attribution/archive").glob("*/demo_mock.jsonl"))
            assert (
                len(archived) == 1
                and json.loads(archived[0].read_text().splitlines()[-1])["worker_id"] == "w2"
            )
        finally:
            world.stop()
    finally:
        stub.close()


def test_episode_sid_is_safe_and_distinct_per_episode():
    assert hub_world.episode_sid("cort", "ep1") == "cort-ep1"
    assert hub_world.episode_sid("a_b c", "2") == "a-b-c-2"
    assert hub_world.episode_sid("cort", "ep1") != hub_world.episode_sid("cort", "ep2")


@pytest.mark.parametrize("mismatch", ["initial", "current", "missing_current", "boolean"])
def test_start_rejects_incorrect_readback_before_publishing_endpoints(company, mismatch):
    root, folder = company
    write(folder / "world/demo_mock.state.json", SEEDED)
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    stopped = []

    class Client:
        base_url = "http://unused"

        def seed(self, sid, state):
            return {"success": True}

        def inspect(self, sid):
            result = {"initial_state": SEEDED, "current_state": SEEDED, "state_diff": {}}
            if mismatch == "boolean":
                result["current_state"] = {**SEEDED, "currentUser": {**USERS[0], "id": True}}
            elif mismatch == "missing_current":
                del result["current_state"]
            else:
                result[mismatch + "_state"] = {"wrong": True}
            return result

    class Server:
        client = Client()

        def stop(self):
            stopped.append(True)

    world = hub_world.CompanyWorld(folder, root / "hub", root / "work", root=root)
    with pytest.raises(ValueError, match="seeded state"):
        world.start(
            server_factory=lambda _: Server(),
            proxy_factory=lambda *args: pytest.fail("proxy before verification"),
        )
    assert stopped == [True]
    assert not (folder / "runtime/endpoints.json").exists()


def test_top_level_keys_skip_nested_paths():
    from company_envs.world.hub_app import top_level_keys

    schema = "## State Schema\n| key | type |\n| --- | --- |\n| `contracts` | array |\n| `contracts[].parties[]` | array |\n| `tags` | array |\n"
    assert top_level_keys(schema) == ["contracts", "tags"]


def test_record_collections_exclude_ui_state_and_scalars():
    from company_envs.world.hub_app import record_collections

    schema = (
        "## State Schema\n| Key | Type | Description |\n|---|---|---|\n| `items` | object | Map of all items keyed by item ID |\n"
        "| `users` | object | Map of users keyed by user ID |\n| `currentUser` | object | Active user |\n| `viewMode` | string | grid or list |\n"
        "| `sortConfig` | object | `{key, direction}` |\n| `uploadQueue` | array | In-progress uploads |\n| `selectedItems` | array | IDs selected |\n"
        "| `emails` | array | All emails |\n| `settings` | object | Preferences |\n"
    )
    assert record_collections(schema) == ["items", "users", "emails"]


def test_a_worker_url_opens_the_route_that_holds_the_world(company, monkeypatch):
    """``/`` is a launcher or a dashboard in several clones, not the page the records are on.

    Measured on the currently passing renders: google_drive on ``/`` shows 1 of 400 seeded values in
    ~210 characters against 5-26 in 1.3-2.0k on ``/recent``; google_sheets' ``/`` is a "Recent
    spreadsheets" launcher at ~180 characters against up to 15k on ``/spreadsheet``. The render gate
    already opens the entry route, so a worker URL of ``/`` meant the gate and the worker judged
    different pages. This one string is also what hub_vm turns into the guest's bookmark, its
    start-page tile and its startup tab, so they all follow it.
    """
    root, folder = company
    write(folder / "world/demo_mock.state.json", SEEDED)
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    monkeypatch.setattr(hub_world, "entry_routes", lambda: {APP["id"]: "/recent"})
    plan = hub_world.load_world(folder, root)
    assert plan[0]["entry_path"] == "/recent"

    class Client:
        base_url = "http://unused"

        def seed(self, sid, state):
            return {"success": True}

        def inspect(self, sid):
            return {"initial_state": SEEDED, "current_state": SEEDED, "state_diff": {}}

    world = hub_world.CompanyWorld(folder, root / "hub", root / "work", host="127.0.0.1", root=root)
    world.start(
        server_factory=lambda _: type("S", (), {"client": Client(), "stop": lambda self: None})(),
        proxy_factory=lambda *args: type("P", (), {"port": 9999, "stop": lambda self: None})(),
    )
    endpoint = read(folder / "runtime/endpoints.json")["apps"][APP["id"]]
    assert endpoint["entry_path"] == "/recent"
    assert endpoint["workers"]["w1"] == "http://127.0.0.1:9999/recent?sid=acme-ep1"
    world.stop()


def test_the_route_comes_from_the_catalogue_and_never_from_a_table_here():
    # No scheme, host, query or fragment: the session id is appended to the URL separately.
    assert hub_world.entry_path({}, None) == "/"
    assert hub_world.entry_path({}, "/recent") == "/recent"
    assert hub_world.entry_path({"entry_path": "/inbox"}, "/recent") == "/inbox"
    assert hub_world.entry_path({}, "http://elsewhere/") == "/"
    assert hub_world.entry_path({}, "/a?sid=x") == "/"


def test_a_handoff_is_deleted_before_serving_and_after_stopping(company):
    """A stale runtime/endpoints.json hands launch-vms dead ports, and the guest then fails with a
    connection *reset* rather than refused, because QEMU's guestfwd accepts before its host-side
    ``nc`` can fail. One live VM run was spent reading that as a broken app. ``hub-serve --check``
    was one way to leave one behind: it served, wrote the handoff, and stopped.
    """
    root, folder = company
    write(folder / "world/demo_mock.state.json", SEEDED)
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    write(folder / "runtime/endpoints.json", {"apps": {"demo_mock": {"workers": {"w1": "dead"}}}})
    write(folder / "runtime/sessions.json", {"sid": "gone"})

    class Client:
        base_url = "http://unused"

        def seed(self, sid, state):
            return {"success": True}

        def inspect(self, sid):
            return {"initial_state": SEEDED, "current_state": SEEDED, "state_diff": {}}

    world = hub_world.CompanyWorld(folder, root / "hub", root / "work", host="127.0.0.1", root=root)
    world.start(
        server_factory=lambda _: type("S", (), {"client": Client(), "stop": lambda self: None})(),
        proxy_factory=lambda *args: type("P", (), {"port": 9, "stop": lambda self: None})(),
    )
    assert read(folder / "runtime/endpoints.json")["apps"]["demo_mock"]["workers"]["w1"].endswith(
        "?sid=acme-ep1"
    )
    world.stop()
    # Nothing is listening on those ports any more, so the handoff is no longer true of anything.
    assert not (folder / "runtime/endpoints.json").exists()
    assert not (folder / "runtime/sessions.json").exists()


def test_a_worker_is_found_in_a_directory_keyed_on_any_identifier():
    """zhihu_mock's user records carry ``userId`` and neither ``id`` nor ``email``.

    ``appears_in_state`` recognised only those two, so its worker could never satisfy the identity
    gate and ``load_world`` refused to serve any company world holding the app at all.
    """
    record = {"userId": "user7", "nickname": "Zhang"}
    state = {"users": [{"userId": "user7", "nickname": "Zhang"}], "questions": []}
    assert hub_world.appears_in_state(record, state)
    # Keyed by id rather than listed, which is how monday and xiaohongshu hold their directories.
    assert hub_world.appears_in_state(record, {"users": {"user7": {"userId": "user7"}}})
    # A different person with the same shape is still a different person.
    assert not hub_world.appears_in_state(record, {"users": [{"userId": "user8"}]})
    # A record with no identifier at all cannot be matched to anybody.
    assert not hub_world.appears_in_state({"nickname": "Zhang"}, state)


def test_a_pointer_identity_is_planned_and_published_as_an_id(company):
    """clio, linear, monday and xiaohongshu hold the signed-in person as an id into ``users``."""
    root, folder = company
    schema = SCHEMA_WITH_USERS.replace(
        "| `ui` | object | UI state |", "| `ui` | object | UI state |\n| `currentUserId` | string | id |"
    )
    (root / "catalogs/app_schemas/demo_mock.md").write_text(schema)
    manifest = read(folder / "apps.json")
    manifest["apps"][0]["identity_key"] = "currentUserId"
    manifest["apps"][0]["top_level_keys"] = ["currentUser", "tickets", "ui", "users", "currentUserId"]
    write(folder / "apps.json", manifest)
    write(folder / "world/demo_mock.state.json", {**SEEDED, "currentUserId": USERS[0]["id"]})
    write(folder / "world/identities.json", {"w1": {"demo_mock": USERS[0]}, "w2": {"demo_mock": USERS[1]}})
    plan = hub_world.load_world(folder, root)
    assert plan[0]["pointers"] == {"w1": str(USERS[0]["id"]), "w2": str(USERS[1]["id"])}
    view = hub_world.identity_view(plan[0], "w2")
    assert view["serves"] == "worker_id" and view["worker_value"] == str(USERS[1]["id"])
    assert view["repairs_seed"] is False


def test_the_handoff_says_which_apps_the_proxy_repairs_for_a_worker():
    """mailchimp seeds ``user`` as a list and its Layout does ``state.user.firstName[0]``.

    The host crashes on the raw world and a worker VM renders it, because the proxy replaces that key
    with the worker's own record. Both readings are true of their own path, so the handoff says which
    apps the two paths disagree about rather than leaving two verdicts to contradict each other.
    """
    broken = {"identity_key": "user", "canonical_user": ["not", "a", "record"], "pointers": {}}
    assert hub_world.identity_view(broken)["repairs_seed"] is True
    assert hub_world.identity_view(broken)["seeded_type"] == "list"
    fine = {"identity_key": "user", "canonical_user": USERS[0], "pointers": {}}
    assert hub_world.identity_view(fine)["repairs_seed"] is False
    assert hub_world.identity_view({"identity_key": None})["serves"] == "seed"
