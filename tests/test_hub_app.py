"""Generic hub contract: seed, inspect, deny, reset — tested against a protocol stub, no node."""

import inspect
import json
import os
import shutil
import subprocess
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

from company_envs.pipeline import new_run
from company_envs.storage import read
from company_envs.world import hub_app
from company_envs.world.capabilities import (
    APP_KINDS,
    runtime_adapter,
    runtime_mounting,
    validate_runtime_mounting,
)

SCHEMA = """# demo_mock Schema

## State Schema

| Key | Type | Description |
|-----|------|-------------|
| `currentUser` | object | Active user |
| `tickets` | array | Tickets |
| `ui` | object | UI state |

### Ticket

| Field | Type | Description |
|-------|------|-------------|
| `id` | number | Ticket id |
"""
REPO = Path(__file__).resolve().parents[1]
APP = {
    "id": "demo_mock",
    "source": "cua_gym_hub",
    "schema": "apps/demo_mock.md",
    "state_keys": ["currentUser", "tickets", "ui", "id"],
}
STATE = {"currentUser": {"id": 1}, "tickets": [{"id": 7}], "ui": {"activeView": 1}}


class StubHub:
    """Minimal reimplementation of the hub vite plugin routes, per sid."""

    def __init__(self):
        store = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def sid(self):
                return self.path.split("sid=")[-1] if "sid=" in self.path else "_default"

            def do_GET(self):
                route = self.path.split("?")[0]
                entry = store.get(self.sid())
                if route == "/state":
                    state = entry["current"] if entry else None
                    self.reply(
                        200, {"stored_state": state, "has_custom_state": state is not None, "sid": self.sid()}
                    )
                elif route == "/go":
                    initial = entry["initial"] if entry else {}
                    current = entry["current"] if entry else initial
                    diff = {
                        k: {"old": initial.get(k), "new": current.get(k)}
                        for k in current
                        if initial.get(k) != current.get(k)
                    }
                    self.reply(200, {"initial_state": initial, "current_state": current, "state_diff": diff})
                else:
                    body = b'<html><body><div id="root">app</div></body></html>'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(size) or b"{}")
                action = data.get("action", "set")
                sid = self.sid()
                if action == "reset":
                    store.pop(sid, None)
                    self.reply(200, {"success": True})
                elif action == "set":
                    store[sid] = {"initial": data["state"], "current": data["state"]}
                    self.reply(200, {"success": True})
                elif action == "set_current":
                    entry = store.setdefault(sid, {"initial": data["state"], "current": data["state"]})
                    entry["current"] = data["state"]
                    self.reply(200, {"success": True})
                else:
                    self.reply(400, {"error": "Unknown action"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    server = StubHub()
    yield server
    server.close()


def test_state_contract_uses_schema_top_level_keys_not_flattened_catalog_names():
    assert hub_app.top_level_keys(SCHEMA) == ["currentUser", "tickets", "ui"]
    assert hub_app.validate_state(APP, STATE, SCHEMA) is STATE
    with pytest.raises(ValueError, match="missing documented keys"):
        hub_app.validate_state(APP, {"currentUser": {}, "tickets": []}, SCHEMA)
    with pytest.raises(ValueError, match="undocumented keys"):
        hub_app.validate_state(APP, {**STATE, "id": 3}, SCHEMA)
    # Without a schema table only the flattened name list is available: catch foreign keys.
    with pytest.raises(ValueError, match="absent from the documented state"):
        hub_app.validate_state(APP, {"tickets": [], "bogus": 1})
    with pytest.raises(TypeError):
        hub_app.validate_state(APP, [], SCHEMA)


def test_hub_apps_are_only_schema_backed_hub_entries():
    apps = [
        APP,
        {**APP, "id": "no_schema", "schema": ""},
        {**APP, "id": "odoo_env", "source": "gym_anything"},
    ]
    assert list(hub_app.hub_apps(apps)) == ["demo_mock"]
    with pytest.raises(ValueError):
        hub_app.validate_sid("bad sid/with space")


def test_seed_inspect_update_reset_roundtrip(stub):
    client = hub_app.HubClient(stub.url)
    client.wait_ready(seconds=5)
    client.seed("world1", STATE)
    go = client.inspect("world1")
    assert go["initial_state"] == STATE and go["current_state"] == STATE and go["state_diff"] == {}
    client.update("world1", {**STATE, "tickets": []})
    go = client.inspect("world1")
    assert go["initial_state"] == STATE and "tickets" in go["state_diff"]
    client.reset("world1")
    assert client.current("world1")["has_custom_state"] is False


def test_worker_proxy_denies_harness_routes_but_passes_app_saves_and_pages(stub):
    client = hub_app.HubClient(stub.url)
    client.seed("w", STATE)
    with hub_app.WorkerProxy(stub.url, host="127.0.0.1") as proxy:
        worker = hub_app.HubClient(f"http://127.0.0.1:{proxy.port}")
        for call in (lambda: worker.inspect("w"), lambda: worker.seed("w", STATE), lambda: worker.reset("w")):
            with pytest.raises(HTTPError) as denied:
                call()
            assert denied.value.code == 403
        # The app loads its seeded session through /state on first visit; workers must reach it.
        assert worker.current("w")["has_custom_state"] is True
        assert worker.update("w", {**STATE, "ui": {}})["success"] is True
        assert client.inspect("w")["current_state"]["ui"] == {}
    assert hub_app.worker_allowed("GET", "/files/w/a.pdf")
    assert hub_app.worker_allowed("POST", "/upload?sid=w")
    assert not hub_app.worker_allowed("POST", "/post?sid=w", {"action": "set", "state": {}})
    assert not hub_app.worker_allowed("GET", "/go?sid=w")


def test_hub_adapter_treats_every_listed_app_as_its_own_origin():
    config = {
        "design": {"runtime_adapter": "hub", "available_runtime_apps": ["Zendesk_mock", "quickbooks_mock"]}
    }
    facts = runtime_mounting(config)
    assert facts["adapter"] == "hub" and facts["identity"] == "shared_session"
    assert set(facts["apps"]) == {"Zendesk_mock", "quickbooks_mock"} and facts["unmapped_app_ids"] == []
    validate_runtime_mounting(["Zendesk_mock", "quickbooks_mock", "contractbook_mock"], adapter="hub")
    # Native stays exactly as before for frozen runs without the field.
    assert runtime_adapter({}) == "native"
    assert runtime_mounting({"design": {"available_runtime_apps": list(APP_KINDS)}})["adapter"] == "native"
    with pytest.raises(ValueError, match="root mount"):
        validate_runtime_mounting(["contractbook_mock", "Zendesk_mock"])
    with pytest.raises(ValueError, match="runtime_adapter"):
        runtime_adapter({"design": {"runtime_adapter": "docker"}})


def test_new_run_rejects_non_hub_apps_under_hub_adapter_and_accepts_the_full_hub_surface(root):
    config = tomllib.loads((root / "config.toml").read_text())
    assert config["design"]["runtime_adapter"] == "hub"
    hub_ids = set(hub_app.hub_apps(read(root / "catalogs" / "apps.json")["apps"]))
    assert set(config["design"]["available_runtime_apps"]) <= hub_ids
    assert len(config["design"]["available_runtime_apps"]) >= 80
    text = (root / "config.toml").read_text()
    (root / "config.toml").write_text(
        text.replace("available_runtime_apps = [\n", 'available_runtime_apps = [\n  "odoo_crm_env",\n', 1)
    )
    with pytest.raises(ValueError, match="not seedable through the hub contract"):
        new_run(root, 1, 2, 1, 0)
    assert not (root / "runs").exists() or not any((root / "runs").iterdir())


def test_hub_process_reinstalls_node_modules_when_dist_is_kept(tmp_path, monkeypatch):
    built = tmp_path / "demo_mock"
    (built / "dist").mkdir(parents=True)
    (built / "dist" / "index.html").write_text("<html></html>")
    (built / "package-lock.json").write_text("{}")
    vite = built / "node_modules" / "vite" / "bin" / "vite.js"
    calls = []

    def fake_run(command, cwd, **kwargs):
        calls.append(command)
        if command[:2] == ["npm", "ci"]:
            # First attempt fails (e.g. lockfile drift); the install fallback restores vite.
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "ci failed"})()
        vite.parent.mkdir(parents=True)
        vite.write_text("// vite")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hub_app.subprocess, "run", fake_run)
    process = hub_app.HubProcess(built, port=1)
    assert process.ensure_node_modules() is True
    assert [c[:2] for c in calls] == [["npm", "ci"], ["npm", "install"]]
    assert all("--ignore-scripts" in c and "--no-audit" in c and "--no-fund" in c for c in calls)
    assert vite.is_file()
    # Installed already: nothing runs.
    calls.clear()
    assert process.ensure_node_modules() is False
    assert calls == []
    # No dist means nothing to serve; do not silently install into an unbuilt directory.
    unbuilt = tmp_path / "unbuilt"
    unbuilt.mkdir()
    with pytest.raises(ValueError, match="dist/index.html"):
        hub_app.HubProcess(unbuilt, port=1).ensure_node_modules()


def test_probe_key_is_a_real_top_level_key_of_the_seed():
    assert hub_app.probe_key(APP, STATE) == "currentUser"
    # Catalog name lists can start with a nested or mis-parsed name; never KeyError on it.
    outlook_like = {**APP, "state_keys": ["inbox", "folders", "messages"]}
    assert hub_app.probe_key(outlook_like, {"folders": [], "messages": []}) == "folders"
    assert hub_app.probe_key({**APP, "state_keys": ["bogus"]}, {"ui": {}}) == "ui"


@pytest.mark.parametrize(
    "route", ["/go/", "/go.json", "/x/../go", "/%67o", "/post/reset", "/post.json", "/state/extra"]
)
def test_worker_route_aliases_cannot_bypass_harness_or_identity(route):
    assert not hub_app.worker_allowed("POST" if "post" in route else "GET", route, {"action": "reset"})


@pytest.mark.parametrize("damage", ["source", "dist", "marker", "patch"])
def test_build_cache_validates_cached_files_and_patch_application(tmp_path, monkeypatch, damage):
    source, target = tmp_path / "demo_mock", tmp_path / "built"
    source.mkdir()
    (source / "vite.config.js").write_text("old source")
    calls = []
    monkeypatch.setattr(
        hub_app,
        "patches",
        lambda _: [{"file": "vite.config.js", "old": "old source", "new": "patched source"}],
    )

    def run(command, cwd, **kwargs):
        calls.append(command)
        if command[0] == "node":
            (cwd / "dist").mkdir()
            (cwd / "dist/index.html").write_text((cwd / "vite.config.js").read_text())

    monkeypatch.setattr(hub_app.subprocess, "run", run)
    hub_app.build(source, target)
    hub_app.build(source, target)
    assert len(calls) == 2
    if damage == "source":
        (target / "vite.config.js").write_text("tampered")
    elif damage == "dist":
        (target / "dist/index.html").write_text("tampered")
    elif damage == "marker":
        (target / ".hub-build.json").write_text("{")
    else:
        (target / "vite.config.js").write_text("old source")
    hub_app.build(source, target)
    assert len(calls) == 4
    served = (target / "dist/index.html").read_text()
    assert served.endswith("patched source")  # the rebuild re-applied the patch
    assert served.count(hub_app.STORAGE_SHIM) == 1  # and re-installed the storage shim


def test_build_rejects_unhashed_symlink_directories(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.js").write_text("unhashed build input")
    (source / "linked").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        hub_app.source_hash(source)


@pytest.mark.parametrize("location", ["landing", "?page=2", "#top"])
def test_proxy_redirect_preserves_safe_relative_locations(location):
    assert hub_app.proxy_redirect(302, b"", {"Location": location})[0] == 302


def test_a_save_that_drops_most_seeded_records_is_refused():
    from company_envs.world.hub_identity import mass_removal

    before = {"documents": [{"id": f"doc-{i}", "title": "t"} for i in range(20)], "ui": {"open": "doc-1"}}
    demo = {
        "documents": [{"id": "doc-1", "title": "Project Proposal"}, {"id": "doc-2", "title": "Meeting Notes"}]
    }
    assert mass_removal(before, demo) == [{"collection": "documents", "before": 20, "removed": 18}]
    edit = {"documents": before["documents"][:-3] + [{"id": "doc-new", "title": "n"}]}
    assert mass_removal(before, edit) == []  # deleting a few records is ordinary editing
    small = {"tags": [{"id": str(i)} for i in range(3)]}
    assert mass_removal(small, {"tags": []}) == []  # too small to call a reset
    sheets = {"sheets": [{"id": f"sheet-{i}"} for i in range(5)]}
    demo = {"sheets": [{"id": "Sheet1"}]}
    assert mass_removal(sheets, demo) == [
        {"collection": "sheets", "before": 5, "removed": 5}
    ]  # a workbook reset
    assert mass_removal(sheets, {"sheets": sheets["sheets"][:3]}) == []  # deleting two sheets is editing


def test_seeded_record_ids_round_trip_through_the_identity_proxy_and_a_demo_save_is_refused(stub):
    state = {
        "currentUser": {"id": 1},
        "tickets": [{"id": 7}, {"id": 8}],
        "notes": {"n1": {"id": "n1", "body": "x"}},
        "ui": {"activeView": 1},
    }
    hub_app.HubClient(stub.url).seed("smoke", state)
    verdict = hub_app.proxy_roundtrip(stub.url, "smoke", state)
    assert verdict == {"record_ids_preserved": True, "replacement_refused": True}
    assert hub_app.HubClient(stub.url).current("smoke")["stored_state"] == state
    demo = hub_app.demo_replacement(state)
    assert set(demo["notes"]) == {"demo-0"} and [t["id"] for t in demo["tickets"]] == ["demo-0", "demo-1"]
    assert demo["ui"] == state["ui"] and demo["currentUser"] == state["currentUser"]
    # A seed with no identifiable records has nothing to protect; the refusal check is skipped.
    hub_app.HubClient(stub.url).seed("plain", {"ui": {"a": 1}, "title": "t"})
    assert hub_app.proxy_roundtrip(stub.url, "plain", {"ui": {"a": 1}, "title": "t"}) == {
        "record_ids_preserved": True,
        "replacement_refused": None,
    }


def test_a_records_own_id_field_is_derived_from_the_records_not_a_list_of_spellings():
    """The id vocabulary was `id` and `sys_id`, and 57 collections in 10 apps key on their own name.

    Measured over the 98 smoke fixtures: `_record_ids` read nothing at all for 7 of them, and for
    six of those seven -- asana, microsoft_teams, salesforce, wechat, youtube, zhihu -- the reason
    was only the spelling, because every collection keys on `userId`, `videoId`, `accountId`. Five
    of the six were already on the runtime surface, so `identity_proxy_refuses_demo_replacement` was
    never emitted for salesforce or asana and `replacement()` skipped their collections outright:
    an unhydrated tab saving its demo rows over a company's world would not have been refused, and
    the smoke report said pass because the check was absent rather than satisfied.

    This is the fifth instance of one class tonight -- an id or collection vocabulary narrower than
    the data -- so the fix derives rather than lists. A field qualifies when every record carries
    it, its values are distinct scalars, and its name either is the collection's own record plus an
    id suffix or is id-shaped with at least two records to prove the distinctness.
    """
    from company_envs.world.hub_identity import _record_ids, id_field

    # The literal vocabulary still wins outright, so nothing an app already had can move.
    assert _record_ids([{"id": 7}, {"id": 8}], "tickets") == {"7", "8"}
    assert _record_ids([{"sys_id": "a"}, {"sys_id": "b"}], "incidents") == {"a", "b"}
    # An own-singular key, including the irregular plurals removesuffix("s") gets wrong.
    assert _record_ids([{"videoId": "v1"}, {"videoId": "v2"}], "videos") == {"v1", "v2"}
    assert id_field("opportunities", [{"opportunityId": "o1", "name": "n"}]) == "opportunityId"
    assert id_field("activities", [{"activityId": "a1", "subject": "s"}]) == "activityId"
    assert id_field("timeOffRequests", [{"requestId": "r1"}, {"requestId": "r2"}]) == "requestId"
    # A foreign key is not an identity: several answers share an authorId and none shares an answerId.
    answers = [{"answerId": "a1", "authorId": "u1"}, {"answerId": "a2", "authorId": "u1"}]
    assert id_field("answers", answers) == "answerId"
    assert _record_ids(answers, "answers") == {"a1", "a2"}
    # Prose and counters are never identities, however unique they happen to be in one fixture.
    assert (
        id_field("dailyMetrics", [{"clicks": 5, "impressions": 9}, {"clicks": 6, "impressions": 8}]) is None
    )
    assert id_field("stations", [{"code": "BJP", "name": "a"}, {"code": "SHH", "name": "b"}]) is None
    # A two-letter stem must not claim an id-shaped field: `ads` is not the owner of `leadId`.
    assert id_field("ads", [{"leadId": "l1", "spend": 1}]) is None
    # One record cannot prove distinctness, so only a name-based claim stands at that size. These
    # three are the foreign keys the looser rule picked, each of which would have had replacement()
    # refuse a worker for re-pointing a record at another parent.
    assert id_field("ossBuckets", [{"name": "b", "regionId": "r"}]) is None
    assert id_field("namedRanges", [{"name": "r", "sheetId": "s"}]) is None
    assert id_field("recentSearches", [{"destination": "d", "destinationId": "d1"}]) is None
    # A map collection's keys are its ids, whatever the records inside are called.
    assert _record_ids({"v1": {"videoId": "v1"}, "v2": {"videoId": "v2"}}, "videos") == {"v1", "v2"}
    assert _record_ids([], "videos") == set() and _record_ids("text", "title") == set()


def test_the_demo_probe_swaps_the_field_the_reader_reads(stub):
    """The probe wrote a literal `id` beside an untouched `videoId`, so it replaced nothing.

    `demo_replacement` builds the save an unhydrated tab would make, and `replacement()` refuses a
    save only when every seeded id is gone. Adding a field the app does not have left all 54 seeded
    videoIds still present, so for any app keyed on its own singular the probe was not a
    replacement -- and a real demo save, which keeps the app's own field names, was exactly the case
    the reader could not see either.
    """
    from company_envs.world.hub_identity import refusal

    state = {
        "videos": [{"videoId": "v1", "title": "a"}, {"videoId": "v2", "title": "b"}],
        "ui": {"tab": "home"},
    }
    demo = hub_app.demo_replacement(state)
    assert [r["videoId"] for r in demo["videos"]] == ["demo-0", "demo-1"]
    assert "id" not in demo["videos"][0]  # the probe does not invent a field the app never had
    assert demo["ui"] == state["ui"]
    assert refusal(state, demo)["refused"] == "demo_state"
    hub_app.HubClient(stub.url).seed("youtube", state)
    assert hub_app.proxy_roundtrip(stub.url, "youtube", state) == {
        "record_ids_preserved": True,
        "replacement_refused": True,
    }


def test_every_fixture_but_the_one_with_no_records_can_see_a_demo_replacement():
    """97 of 98 on 2026-09-10, up from 91: the check has to run before it can pass.

    canvas_mock is the one exception and is correct: its whole state is
    {canvasJSON, canvasImage, timestamp} with no record collections at all, so there is no identity
    to protect. That is structurally different from the six apps whose records were invisible only
    because of how they spell their keys, and conflating the two hid the real defect behind a
    benign one.
    """
    from company_envs.world.hub_identity import refusal

    blind, unrefused = [], []
    for path in sorted((REPO / "experiments/hub-smoke/states").glob("*.json")):
        state = json.loads(path.read_text())
        if not hub_app.record_id_sets(state):
            blind.append(path.stem)
        elif refusal(state, hub_app.demo_replacement(state)) is None:
            unrefused.append(path.stem)
    assert blind == ["canvas_mock"], blind
    assert unrefused == [], unrefused


TABLEAU_APP = {
    "id": "tableau_mock",
    "source": hub_app.HUB_SOURCE,
    "schema": "catalogs/app_schemas/tableau_mock.md",
    "state_keys": [
        "workbook",
        "dataSources",
        "worksheets",
        "dashboards",
        "calculatedFields",
        "parameters",
        "uiState",
        "currentUser",
    ],
}


def tableau_state(chart_data, **sheet):
    return {
        "workbook": {"id": "wb-1", "name": "Sales", "sheetOrder": ["ws-1"], "activeSheetId": "ws-1"},
        "dataSources": [],
        "worksheets": [
            {
                "id": "ws-1",
                "type": "worksheet",
                "name": "By supplier",
                "dataSourceId": "ds-1",
                "columns": [{"fieldId": "f-s", "fieldName": "Supplier", "isDiscrete": True}],
                "rows": [{"fieldId": "f-c", "fieldName": "Cost", "aggregation": "AVG", "isDiscrete": False}],
                "filters": [],
                "pages": [],
                "marks": {"markType": "Bar"},
                "showMeType": "bar",
                "chartData": chart_data,
                **sheet,
            }
        ],
        "dashboards": [],
        "calculatedFields": [],
        "parameters": [],
        "uiState": {"sidebarTab": "data", "activeView": "worksheet"},
        "currentUser": {"name": "Ibrahim Sethi", "role": "Creator"},
    }


ROW_CHART = [{"Supplier": "Alder Film", "Cost": 0.2}, {"Supplier": "Brook", "Cost": 0.14}]
OBJECT_CHART = {"type": "bar", "categories": ["A", "B"], "series": [{"name": "Cost", "values": [0.2, 0.14]}]}


@pytest.mark.parametrize("chart", [ROW_CHART, OBJECT_CHART, []])
def test_tableau_seed_accepts_both_chart_data_forms(chart):
    schema = (REPO / TABLEAU_APP["schema"]).read_text()
    state = tableau_state(chart)
    assert hub_app.validate_state(TABLEAU_APP, state, schema) is state
    assert hub_app.validate_state(TABLEAU_APP, state) is state  # nested checks run without a schema too


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (tableau_state("Supplier,Cost"), "worksheets[0] (ws-1).chartData must be an object"),
        (tableau_state([["Alder Film", 0.2]]), "chartData rows must all be objects"),
        (tableau_state({"type": "bar", "categories": ["A"]}), "needs categories[] and series[]"),
        (
            tableau_state({"type": "bar", "categories": ["A"], "series": [{"name": "Cost"}]}),
            "{name, values[]}",
        ),
        (tableau_state(ROW_CHART, columns=[{"fieldId": "f-s"}]), "columns must be a list of pills"),
        (tableau_state(ROW_CHART, id=""), "needs a non-empty string id"),
        (
            {**tableau_state(ROW_CHART), "workbook": {"name": "Sales"}},
            "workbook must be an object with a non-empty",
        ),
        (
            {**tableau_state(ROW_CHART), "currentUser": {"role": "Creator"}},
            "currentUser must be an object with",
        ),
        ({**tableau_state(ROW_CHART), "uiState": []}, "uiState must be an object"),
    ],
)
def test_tableau_seed_that_would_stall_the_frontend_is_rejected_at_seeding(state, message):
    schema = (REPO / TABLEAU_APP["schema"]).read_text()
    with pytest.raises(ValueError, match="stay on Loading") as rejected:
        hub_app.validate_state(TABLEAU_APP, state, schema)
    assert message in str(rejected.value)


def hub_root():
    config = tomllib.loads((REPO / "config.toml").read_text())
    return Path(config["design"]["hub_root"]).expanduser()


def patched_copy(tmp_path, app_id):
    """The patch targets of one app copied from the pinned hub checkout, with patches applied."""
    source = hub_root() / app_id
    if not source.is_dir():
        pytest.skip(f"hub source {source} is not checked out")
    target = tmp_path / app_id
    for edit in hub_app.patches(app_id):
        path = target / edit["file"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / edit["file"], path)
    hub_app.apply_patches(target, app_id)
    return target


@pytest.mark.parametrize("app_id", sorted(json.loads(hub_app.PATCHES_PATH.read_text())))
def test_every_recorded_patch_anchor_matches_the_hub_source_once(tmp_path, app_id):
    patched_copy(tmp_path, app_id)  # apply_patches raises when an anchor is missing or ambiguous


@pytest.mark.skipif(shutil.which("node") is None, reason="Running the patched issue numbering requires Node")
@pytest.mark.parametrize("existing, expected", [([], ["RD-1", "RD-2"]), ([7, 280, 19], ["RD-281", "RD-282"])])
def test_jira_creates_distinct_issue_keys_in_a_seeded_project(tmp_path, existing, expected):
    patch = hub_app.patches("jira_mock")[0]
    state = {
        "projects": [{"id": "project-relaydesk", "key": "RD"}],
        "issues": [{"projectId": "project-relaydesk", "key": f"RD-{number}"} for number in existing]
        + [{"projectId": "p1", "key": "DEMO-999"}],
    }
    harness = tmp_path / "issue-keys.mjs"
    harness.write_text(
        "const state = JSON.parse(process.argv[2]);\nfunction createIssue() {\n"
        + patch["new"]
        + "const issue = {projectId: project.id, key: `${project.key}-${maxKeyNum + 1}`};\n"
        + "state.issues.push(issue); return issue.key;\n}\n"
        + "console.log(JSON.stringify([createIssue(), createIssue()]));\n"
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps(state)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == expected


@pytest.mark.skipif(shutil.which("node") is None, reason="Running Jira transition rules requires Node")
@pytest.mark.parametrize("file", ["src/components/IssueModal.tsx", "src/pages/Board.tsx"])
@pytest.mark.parametrize("as_map", [False, True])
@pytest.mark.parametrize("status, expected", [("To Do", ["In Progress"]), ("Done", []), ("Unknown", [])])
def test_jira_issue_and_board_preserve_transition_rules_in_lists_and_maps(
    tmp_path, file, as_map, status, expected
):
    rules = {"To Do": ["In Progress"], "In Progress": ["To Do", "Done"], "Done": []}
    transitions = rules if as_map else [{"from": key, "to": value} for key, value in rules.items()]
    patch = next(p for p in hub_app.patches("jira_mock") if p["file"] == file)
    harness = tmp_path / "transitions.mjs"
    harness.write_text(
        "const workflow = JSON.parse(process.argv[2]);\n"
        "const issue = {status: process.argv[3]}; const editedIssue = issue;\n"
        "const allowed = " + patch["new"] + "\nconsole.log(JSON.stringify(allowed));\n"
    )
    result = subprocess.run(
        ["node", str(harness), json.dumps({"transitions": transitions}), status],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == expected


def test_docs_patches_take_seeds_verbatim_hold_saves_and_keep_sid_on_routes(tmp_path):
    target = patched_copy(tmp_path, "google_docs_mock")
    store = (target / "src/store/initialData.js").read_text()
    assert "const data = deepMergeWithDefaults(defaults, customState);" not in store
    assert (
        "{ ...defaults, ...customState, ui: deepMergeWithDefaults(defaults.ui, customState.ui || null) }"
        in store
    )
    context = (target / "src/context/DocsContext.jsx").read_text()
    assert "const hydrated = useRef(false);" in context
    assert "if (sid) {" not in context  # /state is fetched even without a sid
    assert "hydrated.current = !!customState;" in context
    assert "if (!loading && hydrated.current) {" in context
    assert context.count("hydrated.current = true;") == 1  # the refresh path (state already local)
    for page in ("src/pages/DocumentList.jsx", "src/pages/DocumentEditor.jsx"):
        text = (target / page).read_text()
        assert "const routerNavigate = useNavigate();" in text
        assert "routerNavigate({ pathname: to, search: window.location.search }, options)" in text
        assert text.count("useNavigate()") == 1


def test_sheets_patch_gates_the_mount_time_save_on_hydration(tmp_path):
    target = patched_copy(tmp_path, "google_sheets_mock")
    text = (target / "src/store/useSpreadsheet.ts").read_text()
    assert "const hydrated = useRef(false);" in text
    save_effect = text.split("useEffect(() => {\n    if (!hydrated.current) return;", 1)
    assert len(save_effect) == 2 and "saveState(state, sid, initialStateRecord);" in save_effect[1][:200]
    hydrate = text.index("hydrated.current = true;")
    assert (
        text.index("setInitialStateRecord(JSON.parse(JSON.stringify(data)));")
        < hydrate
        < text.index("} else {", hydrate - 400)
    )
    assert "saving is disabled for this tab" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="Running the patched adapter requires node on PATH")
@pytest.mark.parametrize("chart", [ROW_CHART, OBJECT_CHART])
def test_patched_tableau_adapter_builds_chart_configs_from_both_forms(tmp_path, chart):
    patch = next(p for p in hub_app.patches("tableau_mock") if p["file"] == "src/utils/dataManager.js")
    harness = tmp_path / "adapter.mjs"
    harness.write_text(
        "const createInitialData = () => ({ users: [] });\n"
        + patch["new"]
        + "\n  return customState\n}\n"
        + "const out = initializeData('s', JSON.parse(process.argv[2]));\n"
        + "console.log(JSON.stringify({ workbook: out.workbooks[0], ui: out.uiState }));\n"
    )
    seed = tableau_state(chart, showMeType="circle" if chart is ROW_CHART else "bar")
    result = subprocess.run(
        ["node", str(harness), json.dumps(seed)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    config = out["workbook"]["sheets"][0]["chartConfig"]
    assert config["type"] == "bar"  # "circle" has no chart widget; bar renders instead of a blank
    assert config["bars"] == config["lines"] == config["areas"] == [{"key": "Cost"}]
    assert [row[config["xKey"]] for row in config["data"]] == (
        ["Alder Film", "Brook"] if chart is ROW_CHART else ["A", "B"]
    )
    assert [row["Cost"] for row in config["data"]] == [0.2, 0.14]
    assert out["workbook"]["owner"] == "Ibrahim Sethi" and out["ui"]["selectedSheet"] == "ws-1"


def test_slack_patches_land_on_an_existing_channel_instead_of_channel_not_found(tmp_path):
    target = patched_copy(tmp_path, "slack_mock")
    app = (target / "src/App.jsx").read_text()
    assert "import { AppProvider, useApp } from './context/AppContext';" in app
    redirect = app.split("function RedirectWithQuery({ to }) {", 1)[1].split("\n}\n", 1)[0]
    assert "const { state } = useApp();" in redirect
    assert "if (!state || !state.channels) return null;" in redirect  # wait for the seeded state
    assert "!state.channels.some(ch => ch.channelId === wanted)" in redirect
    assert "state.channels.find(ch => (ch.members || []).includes(me)) || state.channels[0]" in redirect
    assert "return <Navigate to={query ? `${target}?${query}` : target} replace />;" in redirect  # keeps ?sid
    assert '<Route index element={<RedirectWithQuery to="/channel/general" />} />' in app  # default kept
    view = (target / "src/components/ChannelView.jsx").read_text()
    assert "import { useParams, Navigate } from 'react-router-dom';" in view
    fallback = view.split("  if (!channel) {", 1)[1].split("\n  }\n", 1)[0]
    assert (
        "return <Navigate to={`/channel/${first.channelId}${window.location.search}`} replace />;" in fallback
    )
    assert fallback.rstrip().endswith('return <div className="channel-view">Channel not found</div>;')
    assert view.count("Channel not found") == 1  # only the no-channels-at-all case


def test_calendar_patches_remove_the_debug_controls_but_keep_the_go_route(tmp_path):
    target = patched_copy(tmp_path, "google_calendar_mock")
    header = (target / "src/components/Header.jsx").read_text()
    sidebar = (target / "src/components/Sidebar.jsx").read_text()
    assert "Debug API" not in header and "Debug State JSON" not in header
    assert "Debug State" not in sidebar
    assert "Drag and drop events to reschedule." in sidebar  # the neighbouring tip survives
    assert "Quick Add Toggle" in header
    for edit in hub_app.patches("google_calendar_mock"):
        assert "/go" not in edit["new"] and "vite.config" not in edit["file"]


def test_sheets_home_patches_show_only_the_seeded_workbook_and_no_demo_user(tmp_path):
    target = patched_copy(tmp_path, "google_sheets_mock")
    home = (target / "src/pages/Home.tsx").read_text()
    for demo in ("Alex Johnson", "Alex+Johnson", "Marketing Campaign Tracker", "Website Analytics", "<img"):
        assert demo not in home
    recent = home.split("const recentFiles = [", 1)[1].split("];", 1)[0]
    assert recent.count("{ title:") == 1 and "state.title" in recent
    assert "recentFiles.map(" in home  # the list and the file picker still render from it


def test_the_ten_newly_admitted_apps_all_answer_the_state_envelope():
    """12 of the 98 clones answer GET /state with the bare state; 10 carried a fix and 2 did not.

    ``HubClient.current`` raises ``KeyError: 'has_custom_state'`` on a bare answer and
    ``golden.py:337`` reads that key directly, so a company holding such an app dies in golden
    authoring rather than failing a check. The two unpatched ones were ``google_analytics_mock`` and
    ``wandb_mock`` -- exactly the pair that had never been in the catalogue, so nothing had ever run
    them. Counted twice by two agents with two methods: the same 12, the same 10, the same 2.
    """
    patches = json.loads(hub_app.PATCHES_PATH.read_text())
    bare = []
    for app_id in sorted(hub_app.hub_apps(json.loads((REPO / "catalogs/apps.json").read_text())["apps"])):
        source = hub_root() / app_id
        config = next(
            (source / f"vite.config.{e}" for e in ("js", "ts") if (source / f"vite.config.{e}").is_file()),
            None,
        )
        if config is None:
            pytest.skip(f"hub source {source} is not checked out")
        if "has_custom_state" not in config.read_text():
            bare.append(app_id)
    patched = [a for a in bare if any("has_custom_state" in e.get("new", "") for e in patches.get(a, []))]
    assert len(bare) == 12, bare
    assert sorted(patched) == sorted(bare), sorted(set(bare) - set(patched))


def test_analytics_patches_answer_the_envelope_and_stop_unioning_the_seed_into_demo_rows(tmp_path):
    """A 90-day dailyMetrics seed came back as 180 days, 90 of them the clone's own 2024 demo rows.

    ``initializeData`` deep-merged the seed into freshly generated defaults, and ``dailyMetrics`` is
    an object keyed by date (90 days from 2024-09-17), so any company world on other dates unioned
    rather than replaced -- and ``Layout.jsx`` calls ``addRecentlyAccessed`` on mount, which posts
    that merged state back as the current state, so the demo rows reach what a grader reads.
    Measured end to end on a 2026-dated seed: 180 days and 90 demo dates before, 90 and 0 after.
    The shallow spread is what ``openreview_mock`` already does; it keeps the only thing the merge
    was for, a top-level key the seed omits falling back to the clone's default.
    """
    target = patched_copy(tmp_path, "google_analytics_mock")
    manager = (target / "src/utils/dataManager.js").read_text()
    assert "const merged = { ...createInitialData(), ...customState };" in manager
    assert "deepMerge(createInitialData()" not in manager
    assert "if (data && data.has_custom_state) return data.stored_state;" in manager
    config = (target / "vite.config.js").read_text()
    # The envelope handler has to register inside the preview server, ahead of the clone's own bare
    # one: vite preview is what HubProcess runs, and the first middleware on a path wins.
    preview = config.index("configurePreviewServer(server) {")
    assert "has_custom_state" in config[preview : config.index("/upload", preview)]
    assert config.count("has_custom_state") == 1  # the dev handler is byte-identical and unpatched


def test_wandb_patches_answer_the_envelope_on_the_preview_server_and_unwrap_it_in_the_client(tmp_path):
    """wandb answered the bare state *and* read the bare state, so one fix without the other breaks it.

    ``hub_identity.rewrite_state`` reads ``stored_state`` to scope a world to the signed-in worker and
    ``write_state`` reads it to rebase a merged save, so without the envelope every worker was served
    the unscoped world and a merged write answered ``502 Cannot read shared state for merge``.
    ``hub_app.smoke`` raised ``KeyError`` rather than failing a check.
    """
    target = patched_copy(tmp_path, "wandb_mock")
    config = (target / "vite.config.js").read_text()
    assert config.count("stored_state") == 2  # the session that has a state, and the one that does not
    assert "has_custom_state: parsed !== null" in config
    manager = (target / "src/utils/dataManager.js").read_text()
    # Still accepts a bare state, so `npm run dev` against the unpatched dev handler keeps working.
    assert "'stored_state' in data ? data.stored_state : data" in manager


def test_westlaw_patches_keep_the_sid_unwrap_the_envelope_and_never_claim_a_baseline(tmp_path):
    """westlaw passed every protocol check while discarding the whole seeded world.

    ``fetchCustomState`` returned the ``/state`` envelope and ``initializeData``'s ``if (custom)``
    always took it, because an object is truthy -- so the app ran with ``state.cases === undefined``
    and never reached its own demo data either. ``smoke`` is blind to that: it exercises the server
    and only looks at the screen when ``--expect`` is given, which 21 of 88 fixtures supplied.

    Unwrapping it exposed two more. ``getSessionId`` read ``?sid=`` on every mount and remembered
    nothing, and react-router links carry no query string, so one click on the Folders nav moved the
    app to the shared ``default`` session: a 5.24 MB world seeded as Ingrid Solberg rendered the
    clone's demo Sarah Mitchell. westlaw was the only one of the five with no sessionStorage sid
    (youtube 4 references, TradingView 2, tripadvisor 2, wandb 2, westlaw 0). And the demo
    fall-through posted ``{action: "set"}`` with fifteen demo cases whenever the ``/state`` read did
    not come back, which for a megabyte world refetched on every navigation is not hypothetical.
    """
    target = patched_copy(tmp_path, "westlaw_mock")
    manager = (target / "src/utils/dataManager.js").read_text()
    assert "sessionStorage.setItem('westlaw_sid', sid)" in manager
    assert "sessionStorage.getItem('westlaw_sid')" in manager
    assert "if (data && data.has_custom_state && data.stored_state) return data.stored_state;" in manager
    claim = manager.split("export async function initializeData(sid) {", 1)[1]
    assert "if (!data || data.has_custom_state) return (data && data.stored_state) || initial;" in claim
    assert claim.index("action: 'set'") > claim.index("has_custom_state")  # the guard precedes the post


def test_tradingview_lock_patch_lets_npm_ci_install_the_tree_the_lock_already_pins():
    """The only dependency collision in 98 clones, and it is a stale recorded peer range.

    ``package.json`` asks for vite ^8.0.0 and the lockfile resolves vite 8.0.0, but the lock records
    ``@vitejs/plugin-react@5.1.4``'s peer range as ending at vite 7, so ``npm ci`` exits with
    ERESOLVE and ``build()`` raises -- measured: unpatched ``npm ci`` fails, patched installs 77
    packages and ``vite build`` transforms 1,768 modules into a 546 kB bundle. Six other clones pair
    vite 8 with plugin-react 6 and install cleanly; this row is the one mismatch.

    ``HubProcess.ensure_node_modules`` already falls back from ``npm ci`` to ``npm install`` and
    ``build()`` does not, so any future clone with an unsatisfiable lock fails here the same way.
    """
    source = hub_root() / "TradingView_mock"
    if not source.is_dir():
        pytest.skip(f"hub source {source} is not checked out")
    edits = hub_app.patches("TradingView_mock")
    assert [e["file"] for e in edits] == ["package-lock.json"]
    wanted = json.loads((source / "package.json").read_text())["devDependencies"]["vite"]
    assert wanted == "^8.0.0"
    assert "^8.0.0" not in edits[0]["old"] and "^8.0.0" in edits[0]["new"]
    # Only the recorded peer range moves; the resolved versions the lock pins are untouched.
    assert edits[0]["old"].startswith('"vite": "') and edits[0]["new"].startswith(edits[0]["old"][:-1])


def test_storage_shim_precedes_every_app_script_and_is_written_once(tmp_path):
    """A company's records outgrow the browser's storage quota; the cache write must not throw."""
    for name, html in (
        ("with_head", "<!doctype html><html><head><title>t</title></head><body><script src=a.js>"),
        ("no_head", "<!doctype html>\n<html lang=en>\n<body>\n<script>start()</script>\n"),
        ("no_script", "<!doctype html><html><body>nothing to run</body></html>"),
    ):
        target = tmp_path / name
        target.mkdir()
        (target / "index.html").write_text(html)
        assert hub_app.inject_storage_shim(target) is True
        assert hub_app.inject_storage_shim(target) is False  # idempotent: one shim per build
        text = (target / "index.html").read_text()
        assert text.count(hub_app.STORAGE_SHIM) == 1
        assert "<script" not in text[: text.index(hub_app.STORAGE_SHIM)]  # nothing runs first


def test_storage_shim_version_is_part_of_the_build_identity(monkeypatch):
    """A changed shim has to rebuild every app, the way a changed patch does.

    This used to assert that the name STORAGE_SHIM_VERSION appeared in build's source -- which
    passed whether or not anyone remembered to bump it. The amendment text is now its own key,
    so the test can ask the real question: edit the shim, and the build identity moves.
    """
    source = inspect.getsource(hub_app.build)
    assert "amendment_hash()" in source
    assert source.index("inject_storage_shim") > source.index("apply_patches(target, app_id)")
    before = hub_app.amendment_hash()
    monkeypatch.setattr(hub_app, "STORAGE_SHIM", hub_app.STORAGE_SHIM + "<!-- edited -->")
    assert hub_app.amendment_hash() != before


@pytest.mark.parametrize("app_id", sorted(json.loads(hub_app.PATCHES_PATH.read_text())))
def test_every_patched_hub_app_can_carry_the_storage_shim(tmp_path, app_id):
    source = hub_root() / app_id
    if not (source / "index.html").is_file():
        pytest.skip(f"hub source {source} is not checked out")
    target = tmp_path / app_id
    target.mkdir()
    shutil.copyfile(source / "index.html", target / "index.html")
    assert hub_app.inject_storage_shim(target) is True
    text = (target / "index.html").read_text()
    assert "<script" not in text[: text.index(hub_app.STORAGE_SHIM)]  # nothing runs first
    assert "<script" in text[text.index(hub_app.STORAGE_SHIM) + len(hub_app.STORAGE_SHIM) :]


def test_a_key_the_app_keeps_is_seedable_without_rejecting_the_worlds_that_exist():
    """A key the imported schema omits can never be seeded, so the app shows its own demo records
    and writes them into the company's world. The schema documents are imported upstream and
    pinned by hash, so the supplement records the observed keys instead, the way hub_patches.json
    records source amendments. They are permitted, never required: documenting 101 such keys as
    required would reject every world that already exists.
    """
    schema = """## State Schema

| Key | Type | Description |
|-----|------|-------------|
| `tickets` | array | Each: `{id, subject}` |
"""
    supplement = {"demo_mock": {"keys": {"employees": {"kind": "array"}}}}
    assert hub_app.top_level_keys(schema) == ["tickets"]
    assert hub_app.permitted_keys("demo_mock", schema, supplement) == ["tickets", "employees"]
    assert hub_app.permitted_keys("other_mock", schema, supplement) == ["tickets"]
    app = {"id": "demo_mock", "state_keys": ["tickets", "employees"]}
    monkey = hub_app.schema_supplement
    try:
        hub_app.schema_supplement = lambda *a, **k: supplement
        hub_app.validate_state(app, {"tickets": []}, schema)  # the supplement key may be absent
        hub_app.validate_state(app, {"tickets": [], "employees": []}, schema)  # and may be present
        with pytest.raises(ValueError, match="missing documented keys"):
            hub_app.validate_state(app, {"employees": []}, schema)
        with pytest.raises(ValueError, match="undocumented keys"):
            hub_app.validate_state(app, {"tickets": [], "invented": 1}, schema)
    finally:
        hub_app.schema_supplement = monkey


def test_the_supplement_only_names_keys_the_imported_schemas_leave_out():
    """A supplement row that duplicates the document, or names an app with no schema, is stale."""
    catalog = hub_app.hub_apps(json.loads((REPO / "catalogs/apps.json").read_text())["apps"])
    for app_id, entry in hub_app.schema_supplement().items():
        assert app_id in catalog, f"{app_id} is in the supplement but not a hub app"
        schema = REPO / "catalogs/app_schemas" / Path(catalog[app_id]["schema"]).name
        documented = set(hub_app.top_level_keys(schema.read_text()) or [])
        overlap = sorted(set(entry["keys"]) & documented)
        assert not overlap, f"{app_id} supplements keys its schema already documents: {overlap}"
        assert entry.get("reason"), f"{app_id} supplement has no recorded reason"


def test_a_live_server_is_never_rebuilt_or_served_out_from_under(tmp_path, monkeypatch):
    """Two servers on one build directory delete each other's worlds, silently.

    The app keeps its sessions in `.mock-states` inside the build directory, HubProcess removes
    that on stop, and build() removes the whole directory to rebuild. The app's own /post handler
    ignores the failed write and still answers success, so a wiped world goes on reporting that it
    was seeded while serving the demo records it ships with. Live-VM runs measured 14 of 17 and 9
    of 22 served worlds wiped mid-run this way. batch_queue.prepare_work hardlinks a private build
    per company for exactly this reason; nothing stopped anybody else.
    """
    source, target = tmp_path / "demo_mock", tmp_path / "built"
    source.mkdir()
    (source / "vite.config.js").write_text("source")
    monkeypatch.setattr(hub_app, "patches", lambda _: [])

    def run(command, cwd, **kwargs):
        if command[0] == "node":
            (cwd / "dist").mkdir(exist_ok=True)
            (cwd / "dist/index.html").write_text("<html><body>built</body></html>")

    monkeypatch.setattr(hub_app.subprocess, "run", run)
    hub_app.build(source, target)

    assert hub_app.serving_pid(target) is None  # nothing is serving it
    (target / hub_app.SERVING_MARKER).write_text(str(os.getpid()))
    assert hub_app.serving_pid(target) == os.getpid()
    (source / "vite.config.js").write_text("changed, so the cache is stale")
    with pytest.raises(ValueError, match="is being served by pid"):
        hub_app.build(source, target)
    assert (target / "dist/index.html").is_file(), "the refusal destroyed nothing"

    # A marker left by a process that has gone is not a claim on anything.
    (target / hub_app.SERVING_MARKER).write_text("999999999")
    assert hub_app.serving_pid(target) is None
    hub_app.build(source, target)


def test_a_server_that_does_not_own_the_build_leaves_its_sessions_alone(tmp_path):
    built = tmp_path / "built"
    (built / ".mock-states").mkdir(parents=True)
    (built / ".mock-states" / "live.json").write_text("{}")
    (built / hub_app.SERVING_MARKER).write_text(str(os.getpid()))
    server = hub_app.HubProcess(built)
    server.stop()  # never started, so it owns nothing
    assert (built / ".mock-states" / "live.json").is_file()
    assert (built / hub_app.SERVING_MARKER).is_file()


def test_a_write_that_did_not_land_stops_reporting_success(tmp_path):
    """writeState returns false when the write fails, and 274 call sites across 87 apps threw that
    away and answered {"success": true} regardless. A world whose session directory had gone went
    on reporting itself seeded while serving the demo records the app ships with: cloudflare_mock
    answered success with 32,763 bytes of demo data where 5,120,814 bytes of company records had
    been, and /go named that as both initial and current.
    """
    target = tmp_path / "app"
    target.mkdir()
    (target / "vite.config.js").write_text(
        # The amendment only applies where writeState reports failure by returning.
        "function writeState(sid, state) {\n"
        "  try { fs.writeFileSync(getStateFile(sid), JSON.stringify(state)); return true }\n"
        "  catch (e) { return false }\n"
        "}\n"
        "            if (action === 'set') {\n"
        "              writeState(sid, newState)\n"
        "              writeState(sid, ns);\n"  # a trailing semicolon is still a discarded result
        "              const stored = writeState(sid, other)\n"  # already read, so left alone
        "              if (!writeState(sid, third)) return\n"  # already checked
        "              res.end(JSON.stringify({ success: true }))\n"
        "            }\n"
    )
    assert hub_app.assert_state_writes(target) == 2
    text = (target / "vite.config.js").read_text()
    assert text.count("state write did not land") == 2
    assert "const stored = writeState(sid, other)" in text
    assert "if (!writeState(sid, third)) return" in text
    assert hub_app.assert_state_writes(target) == 0  # idempotent: one amendment per build


def test_a_server_whose_write_throws_is_left_alone(tmp_path):
    """Two apps let the filesystem error propagate and return nothing, so their failures were
    already loud. Wrapping those in `if (!writeState(...))` made *every* write throw, because
    `!undefined` is true -- which turned their /post into a 400 and broke both apps outright. Caught
    by re-running all 88 apps against the committed baseline, not by the suite.
    """
    throws = tmp_path / "throws"
    throws.mkdir()
    (throws / "vite.config.js").write_text(
        "function writeState(sid, data) {\n"
        "  ensureStateDir();\n"
        "  fs.writeFileSync(getStatePath(sid), JSON.stringify(data), 'utf-8');\n"
        "}\n"
        "              writeState(sid, newState)\n"
    )
    assert hub_app.assert_state_writes(throws) == 0
    assert "did not land" not in (throws / "vite.config.js").read_text()

    # The usual shape returns a boolean from inside a try block, which a one-level pattern cannot
    # see -- matching by pattern skipped all 88 apps before this was brace-matched.
    returns = tmp_path / "returns"
    returns.mkdir()
    (returns / "vite.config.js").write_text(
        "function writeState(sid, state) {\n"
        "  try { fs.writeFileSync(getStateFile(sid), JSON.stringify(state)); return true }\n"
        "  catch (e) { return false }\n"
        "}\n"
        "              writeState(sid, newState)\n"
    )
    assert hub_app.assert_state_writes(returns) == 1
    assert "state write did not land" in (returns / "vite.config.js").read_text()


def test_the_write_assertion_is_part_of_the_build_identity(monkeypatch):
    """Widening the write assertion rebuilds every app, without anyone bumping a number."""
    source = inspect.getsource(hub_app.build)
    assert "amendment_hash()" in source
    assert "assert_state_writes(target)" in source
    before = hub_app.amendment_hash()
    monkeypatch.setattr(hub_app, "ASSERT_MESSAGE", hub_app.ASSERT_MESSAGE + " (widened)")
    assert hub_app.amendment_hash() != before


def test_calendar_month_view_paints_a_seeded_hex_colour_instead_of_dropping_it(tmp_path):
    """An event colour used as a CSS class beside text-white is white text on white.

    Measured in a guest: 7 of 7 chips had background-color rgba(0,0,0,0) and the month grid
    screenshot was empty, with 297 of 297 seeded events invisible. Fleet-wide, 11,731 of 17,465
    events carry a hex colour. The same fix had already landed in Sidebar.jsx and missed this view.
    """
    target = patched_copy(tmp_path, "google_calendar_mock")
    text = (target / "src/components/MonthView.jsx").read_text()
    assert 'style={/^#/.test(event.color || "") ? { backgroundColor: event.color } : undefined}' in text
    # A Tailwind class still reaches className; a hex no longer does.
    assert '/^#/.test(event.color || "") ? "" : (event.color || "bg-blue-500")' in text
    assert 'event.color || "bg-blue-500",\n                      "text-white"' not in text


def test_drive_my_drive_lists_the_company_drive_not_only_what_the_viewer_owns(tmp_path):
    """My Drive filtered the whole tree by ownerId, so a worker's drive came back empty.

    Measured: 18 of 40 worker/company pairs saw zero top-level folders -- all four prx workers,
    because its six roots belong to a non-worker id, and 5 of 6 schweitzer workers. The files were
    only reachable through "Shared with me" and search. Drive is the most-seeded app in the cohort
    (60 of 99 companies), and one patch repairs all of them.
    """
    target = patched_copy(tmp_path, "google_drive_mock")
    context = (target / "src/context/FileSystemContext.tsx").read_text()
    contents = context.split("  const getFolderContents = ", 1)[1].split("\n  };", 1)[0]
    assert "item.ownerId === state.currentUser.id" not in contents  # the emptying clause
    assert "item.parentId === folderId &&" in contents and "!item.trashed &&" in contents
    # A root item that "Shared with me" lists is still not listed twice.
    assert "(folderId !== null ||\n          item.ownerId === me ||" in contents
    assert "!(item.sharedWith || []).some(s => s.userId === me))" in contents
    # Still scoped by owner where the real product scopes it.
    assert "item.ownerId !== state.currentUser.id &&" in context  # getSharedWithMeItems


def test_drive_takes_the_seed_verbatim_instead_of_merging_the_demo_items_into_it(tmp_path):
    """deepMergeWithDefaults recursed into items and users, both keyed objects.

    Every seeded world therefore carried the 22 shipped demo items ('Work Projects', owner
    user_001 'Alex Johnson') and 5 demo users beside the company's records -- 2,531 of them in the
    largest world. The owner filter above was all that kept them off the screen, so the two
    defects had to be fixed together.
    """
    target = patched_copy(tmp_path, "google_drive_mock")
    text = (target / "src/lib/mockData.ts").read_text()
    assert "const data = { ...getDefaultData(), ...customState } as AppState;" in text
    assert "deepMergeWithDefaults(getDefaultData(), customState)" not in text
    assert "cache(sk, JSON.stringify(data));" in text  # the quota-safe cache write still runs


def test_drive_saves_a_workers_edit_instead_of_resetting_the_grader_baseline(tmp_path):
    """On load Drive posted its own items as the session baseline with action:"set".

    worker_allowed admits only set_current -- seeding is harness-only -- so the proxy answered 403,
    the app swallowed it and returned early. Measured in a guest: nine clicks, a typed submit and a
    folder create produced zero further requests and an empty /go diff. The proxy is right to
    refuse, so the save is gated on hydration instead, the way the docs and sheets patches are.
    """
    target = patched_copy(tmp_path, "google_drive_mock")
    text = (target / "src/context/FileSystemContext.tsx").read_text()
    assert "action: 'set'" not in text and "baselineSynced" not in text
    assert "const hydrated = useRef(false);" in text
    assert "if (!loading && state && hydrated.current) {\n      saveState(state, sidRef.current);" in text
    assert "hydrated.current = !!customState;" in text  # a failed /state shows, it does not save
    assert text.count("hydrated.current = true;") == 1  # the refresh path (state already local)
    assert text.index("hydrated.current = !!customState;") < text.index(
        "dispatch({ type: 'SET_STATE', payload: data });\n          setLoading(false);"
    )


def test_sheets_opens_the_workbook_and_keeps_the_launcher_reachable(tmp_path):
    """Sheets routed / to the launcher, whose only seeded string is the workbook title.

    Measured: 177-193 visible characters and 1 of 45-400 seeded strings in every rendered company,
    with the full grid and its 37 tabs one click away. Sheets is the decisive collection for at
    least 6 tasks.
    """
    target = patched_copy(tmp_path, "google_sheets_mock")
    app = (target / "src/App.tsx").read_text()
    assert "import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';" in app
    assert '<Route path="/" element={<OpenTheWorkbook />} />' in app
    assert '<Route path="/home" element={<Home />} />' in app  # the launcher is not deleted
    assert '<Route path="/spreadsheet" element={<Spreadsheet />} />' in app
    landing = app.split("function OpenTheWorkbook() {", 1)[1].split("\n}\n", 1)[0]
    assert "const { search } = useLocation();" in landing
    assert "return <Navigate to={`/spreadsheet${search}`} replace />;" in landing  # keeps ?sid
    grid = (target / "src/pages/Spreadsheet.tsx").read_text()
    assert "Alex Johnson" not in grid  # the share dialog named a demo user as the owner
    assert ">You - Owner</div>" in grid


def test_monday_opens_a_board_the_company_has_instead_of_board_not_found(tmp_path):
    """monday redirected / to /board/board-1, the shipped default's id, which no company has.

    Measured: the first screen read "Board not found" in both seeded worlds, while every seeded
    board renders at its real id (treehouse-foods: board-projects..board-office; eastman-chemical:
    lr-exceptions, lr-improvements). Both worlds set activeBoardId to a board they have.
    """
    target = patched_copy(tmp_path, "monday_mock")
    app = (target / "src/App.jsx").read_text()
    assert '<Route path="/" element={<OpenABoard />} />' in app
    assert "board-1" in app.split("function OpenABoard() {", 1)[1].split("\n}\n", 1)[0]  # last resort
    landing = app.split("function OpenABoard() {", 1)[1].split("\n}\n", 1)[0]
    assert "const { state } = useAppContext();" in landing
    assert "(state.ui && state.ui.activeBoardId) || state.activeBoardId" in landing
    assert "const id = boards[wanted] ? wanted : Object.keys(boards)[0];" in landing
    assert "<RedirectWithQuery to={id ? `/board/${id}` : '/my-work'} />" in landing  # keeps ?sid


def test_a_seeded_hex_colour_paints_instead_of_naming_a_class_in_every_remaining_site(tmp_path):
    """The google_calendar defect, swept: a hex in className is a colour that paints nothing.

    airtable's three seeded worlds carry 75 field-option colours and 3 base colours, all Tailwind
    class names today, so these sites keep a hex working rather than repair a measured blank -- the
    calendar's did both, with 7 of 7 chips at background-color rgba(0,0,0,0). instacart has no
    seeded world and a class-name default. facebook's Post.jsx reaction colour was a false
    positive: it comes from a module constant, never from state.
    """
    for app_id, path, expr in (
        ("airtable_mock", "src/components/Cell.jsx", "selectedOption?.color"),
        ("airtable_mock", "src/components/Cell.jsx", "opt?.color"),
        ("airtable_mock", "src/components/KanbanView.jsx", "col.color"),
        ("airtable_mock", "src/components/Sidebar.jsx", "base.color"),
    ):
        text = (patched_copy(tmp_path / app_id / expr[:3], app_id) / path).read_text()
        assert f'style={{/^#/.test({expr} || "") ? {{ backgroundColor: {expr} }} : undefined}}' in text
        assert f'/^#/.test({expr} || "") ? "" : ({expr} || "bg-' in text  # a class still reaches className
    store = (
        patched_copy(tmp_path / "instacart", "instacart_mock") / "src/pages/StoreSelector.jsx"
    ).read_text()
    assert "${/^#/.test(store.color || '') ? '' : store.color}" in store
    assert "style={/^#/.test(store.color || '') ? { backgroundColor: store.color } : undefined}" in store


def test_facebook_draws_a_person_with_no_avatar_instead_of_an_undefined_icon(tmp_path):
    """facebook rendered 0 visible characters on a seeded world and 3,616 on its own defaults.

    Sidebar draws the signed-in person and every page with src={avatar} and no icon prop; a seeded
    avatar is the empty string, so the fallback rendered <Icon /> with Icon undefined and React
    error #130 took the whole app down -- 0 clickable controls, 0 of 5 sampled seeded strings.
    Bisected by serving the built app one seeded key at a time: currentUser alone and pages alone
    each reproduced it. (The colour-as-class claim against Post.jsx was a false positive, and so was
    a first reading that blamed a missing lucide-react icon: 0.263.1 exports both Edit and Edit2.)
    """
    target = patched_copy(tmp_path, "facebook_mock")
    sidebar = (target / "src/components/Sidebar.jsx").read_text()
    assert "      ) : Icon ? (" in sidebar
    assert "{(text || '?').trim().charAt(0).toUpperCase()}" in sidebar
    assert "<Icon size={36} className={`${color}`} />" in sidebar  # a named icon still wins


def test_instagram_signs_the_worker_in_as_one_of_the_companys_own_people(tmp_path):
    """The signed-in person was a module constant read at 47 sites in 7 files.

    The users merge was a key-wise union, so all ten shipped demo accounts survived in every seeded
    world (50 users where the company has 40) and state.users['user_admin'] stayed resolvable:
    every company was browsed as 'alex_morgan', whose following list hid all 132 seeded posts
    behind five demo accounts' feeds. A live binding names the worker once, for all 47 sites.
    """
    target = patched_copy(tmp_path, "instagram_mock")
    text = (target / "src/utils/mockData.js").read_text()
    assert "export let CURRENT_USER_ID = 'user_admin';" in text  # a live binding, not a constant
    picker = text.split("function pickCurrentUserId(state) {", 1)[1].split("\n}\n", 1)[0]
    assert "state.currentUserId || (state.currentUser && state.currentUser.id)" in picker
    assert "if (users[CURRENT_USER_ID]) return CURRENT_USER_ID;" in picker  # defaults are unchanged
    assert "const first = Object.keys(users)[0];" in picker
    assert text.count("CURRENT_USER_ID = pickCurrentUserId(") == 2  # the seeded load and the refresh
    merge = text.split("      } else if (key === 'users'", 1)[1].split("      } else if", 1)[0]
    assert "const mergedUsers = {};" in merge and "{ ...defaults[key] }" not in merge


def test_zillow_saved_searches_are_ids_and_an_agent_may_have_one_name(tmp_path):
    """Saved Homes read user.savedSearches, a list of id strings, as a list of objects.

    It threw on the app's own default data, where user.savedSearches is ["search-1", "search-2"]
    and the search objects live in state.savedSearches -- which no component read at all. And
    PropertyCard initialled the last name with [1][0], which throws on a one-word agent name and
    takes the home grid, the saved list and every search result down with it.
    """
    target = patched_copy(tmp_path, "zillow_mock")
    saved = (target / "src/pages/SavedHomes.jsx").read_text()
    assert "const savedSearchIds = state.user.savedSearches || [];" in saved
    assert "...(state.savedSearches || []).filter(item => savedSearchIds.includes(item.id))," in saved
    assert "...savedSearchIds.filter(item => item && typeof item === 'object')," in saved  # both forms
    assert "updateFilters(search.filters || {});" in saved
    assert "{(search.filters && search.filters.search) || search.location || 'Any Location'}" in saved
    assert "search.emailAlerts === undefined ? search.alertsEnabled : search.emailAlerts" in saved
    card = (target / "src/components/PropertyCard.jsx").read_text()
    assert "agent.name.split(' ')[1][0]" not in card
    assert ".map((part, index) => (index === 0 ? part : `${part[0]}.`))" in card


def test_zoom_global_search_survives_a_store_that_has_no_channels(tmp_path):
    """Layout wraps every page and its search box read channels, which the store never supplies.

    Typing one character threw on Home, Meetings, Contacts and everywhere else. The guard stops
    that; it does not make Team Chat work. The store has no channels or messages slice and none of
    the seven chat actions TeamChat.jsx calls, so the schema's channels and messages cannot reach
    the screen or be kept -- that belongs to whoever owns the schema, not to a render patch.
    """
    target = patched_copy(tmp_path, "zoom_web_mock")
    layout = (target / "src/components/Layout.jsx").read_text()
    assert "(channels || []).filter(ch => ch.name && ch.name.toLowerCase().includes(q))" in layout


def test_weibo_and_xiaohongshu_keep_the_sid_after_the_first_navigation(tmp_path):
    """syncStateToServer read the sid from window.location.search at save time.

    Neither app preserves the query across its own links -- 41 URL-changing call sites in weibo, 29
    in xiaohongshu -- so every save after the first click posted nowhere and a worker's work never
    reached the grader. Remembered the way monday's getSessionId does, so it also survives a reload
    on a query-less URL.
    """
    for app_id, call in (
        ("weibo_mock", "    const sid = rememberedSid();\n    if (!sid) return;"),
        ("xiaohongshu_mock", "    const sid = rememberedSid();"),
    ):
        target = patched_copy(tmp_path / app_id, app_id)
        text = (target / "src/utils/dataManager.js").read_text()
        assert call in text
        helper = text.split("function rememberedSid() {", 1)[1].split("\n}\n", 1)[0]
        assert "sessionStorage.setItem('mock_sid', fromUrl);" in helper
        assert "return sessionStorage.getItem('mock_sid') || null;" in helper
        assert "new URLSearchParams(window.location.search).get('sid')" in helper
        assert text.count("export function rememberedSid()") == 1
        # A reload after in-app navigation carries no ?sid, so the load path reads the same
        # remembered sid -- otherwise the app would hydrate demo data and post it over the world.
        loader = (target / "src/context/AppContext.jsx").read_text()
        assert "rememberedSid" in loader


def test_weibo_keeps_the_signed_in_persons_own_posts_in_their_own_feed(tmp_path):
    """The following feed's escape hatch tested the shipped demo id 'user_current'.

    A seeded world's signed-in user is one of its own people, so it matched nobody. This is not the
    whole defect: the one seeded weibo world sets isFollowing false on 76 of 76 users while leaving
    ui.feedTab "following", which hides all 253 posts, and the signed-in user wrote none of them.
    The app, its own fixtures and the pinned schema all agree on isFollowing, so that half is the
    seed's to fix.
    """
    target = patched_copy(tmp_path, "weibo_mock")
    home = (target / "src/pages/HomePage.jsx").read_text()
    assert "u.id === (state.currentUser && state.currentUser.id)" in home
    assert "u.isFollowing ||" in home  # the documented semantics are untouched


def node_or_skip():
    if shutil.which("node") is None:
        pytest.skip("Running a patched adapter requires node on PATH")


def run_node(tmp_path, name, source, argument):
    harness = tmp_path / name
    harness.write_text(source)
    result = subprocess.run(
        ["node", str(harness), json.dumps(argument)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


SEEDED_MAIL = {
    "messages": [
        {
            "id": "imp-common-1",
            "conversationId": "imp-common",
            "folderId": "folder-archive",
            "from": {"name": "Harriet Solis", "email": "harriet.solis@northcoastamc.example"},
            "to": [{"name": "Briony Walsh", "email": "briony.walsh@northcoastamc.example"}],
            "subject": "Clarification introduction",
            "body": "The boxes are cramped.",
            "date": "2026-08-18T10:00:00-04:00",
            "isRead": True,
            "isFlagged": False,
            "attachments": [],
        }
    ],
    "folders": [{"id": "folder-inbox", "name": "Inbox"}],
    "categories": [{"id": "category-clinical-review", "name": "Clinical review", "color": "#0078D4"}],
}
DEFAULT_MAIL = {
    "emails": [
        {
            "id": "e1",
            "folderId": "inbox",
            "from": {"name": "Demo", "email": "demo@example.com"},
            "to": [{"name": "Demo User", "email": "you@example.com"}],
            "subject": "Welcome",
            "body": "Hello",
            "preview": "Hello",
            "timestamp": "2026-03-01T09:00:00Z",
            "read": False,
            "flagged": True,
            "categories": [],
            "attachments": [{"name": "a.pdf"}],
        }
    ],
    "folders": [{"id": "inbox", "displayName": "Inbox"}],
}


@pytest.mark.parametrize("state", [SEEDED_MAIL, DEFAULT_MAIL])
def test_outlook_renders_one_mail_list_under_both_field_vocabularies(tmp_path, state):
    """outlook_web rendered a blank white page, seeded and on its own defaults.

    EmailList.jsx:49 reads state.messages with parentFolderId/bodyPreview/receivedDateTime and
    flag.flagStatus; StoreContext.jsx:19 supplies emails with folderId/preview/timestamp/read, and
    FolderPane, ReadingPane and MailRoute read that half. Measured: 0 visible characters, 0 of 5
    sampled seeded strings, 0 clickable controls, on "TypeError: Cannot read properties of undefined
    (reading 'toLowerCase')" -- the seeded searchQuery "August" filtering on e.bodyPreview. Its build
    already carried two half-patches for the same drift.
    """
    node_or_skip()
    patch = next(
        edit for edit in hub_app.patches("outlook_web_mock") if edit["file"] == "src/utils/mockData.js"
    )
    out = run_node(
        tmp_path,
        "adapt.mjs",
        patch["new"].replace("export const adaptMailState", "const adaptMailState", 1)
        + "\nconsole.log(JSON.stringify(adaptMailState(JSON.parse(process.argv[2]))));\n",
        state,
    )
    [mail] = out["messages"]
    assert out["emails"] == out["messages"]  # one list under both names
    assert mail["parentFolderId"] == mail["folderId"]  # the list filters on the first
    assert mail["receivedDateTime"] == mail["timestamp"] == mail["date"]
    assert mail["bodyPreview"] == mail["preview"] and mail["bodyPreview"]
    assert mail["inferenceClassification"] == "focused"  # or the Focused tab shows "Nothing here"
    assert isinstance(mail["body"], str) and isinstance(mail["categories"], list)
    assert mail["toRecipients"] == mail["to"] and len(mail["to"]) >= 1
    assert out["folders"][0]["name"] == out["folders"][0]["displayName"] == "Inbox"
    if state is SEEDED_MAIL:
        assert mail["isRead"] is mail["read"] is True
        assert mail["flag"]["flagStatus"] == "notFlagged" and mail["hasAttachments"] is False
        assert out["categories"][0]["displayName"] == "Clinical review"
    else:
        assert mail["isRead"] is mail["read"] is False
        assert mail["flag"]["flagStatus"] == "flagged" and mail["hasAttachments"] is True


def test_outlook_actions_write_to_the_key_the_state_keeps_its_mail_in(tmp_path):
    """Every mail action read prev.emails, which a schema-backed world does not have.

    A delete, a flag or a read marker threw instead of reaching the state, and deleteEmail moved the
    message to folderId 'deleted' -- an id none of the seeded folders use (they are folder-deleted),
    so the message left every view. EmailList also called seven actions that did not exist at all.
    """
    target = patched_copy(tmp_path, "outlook_web_mock")
    text = (target / "src/context/StoreContext.jsx").read_text()
    assert "prev.emails" not in text
    assert "const mailKey = (prev) => (Array.isArray(prev.messages) ? 'messages' : 'emails');" in text
    assert "return { ...prev, [key]: change(prev[key] || []) };" in text
    assert "const found = (state.folders || []).find(folder => pattern.test(folder.id));" in text
    assert "moveEmail(emailId, folderLike(/deleted|trash/i, 'deleted'))" in text
    for name in ("deleteMessage", "toggleRead", "togglePin", "archiveMessage", "moveMessage"):
        assert name in text, name
    # The components read the adapted view; the save effect still writes the raw seeded shape.
    assert "const view = React.useMemo(() => adaptMailState(state), [state]);" in text
    assert "      state: view," in text
    assert text.index("const view = React.useMemo") < text.index("  if (loading) {")  # hook order
    assert "saveState(state, sidRef.current);" in text
    mail_route = (target / "src/routes/MailRoute.jsx").read_text()
    assert "state.folders.find(folder => /inbox/i.test(folder.id)) || state.folders[0]" in mail_route


TABLEAU_UI_DEFAULTS = {
    "currentPage": "home",
    "selectedWorkbook": None,
    "selectedSheet": None,
    "exploreView": "grid",
    "exploreSort": "name",
    "exploreFilter": "all",
    "exploreSearch": "",
    "sidebarCollapsed": False,
    "adminTab": "users",
    "dashboardFilters": {},
}
SEEDED_FILTER = {
    "fieldId": "fld-supplier",
    "fieldName": "Supplier",
    "filterType": "categorical",
    "selectedValues": ["Alder Film", "Brook"],
    "rangeMin": None,
    "rangeMax": None,
    "showFilter": True,
}


def test_patched_tableau_adapter_maps_a_seeded_filter_and_keeps_the_explore_ui_state(tmp_path):
    """A seeded filter is {fieldId, fieldName, filterType, selectedValues, ...}; the UI reads
    {id, field, values[], selected[]} and threw on all four.

    Measured: one of the two seeded worlds white-screened on the first paint -- every one of CalHR's
    9 worksheets carries filters, so filter.values.map threw in the filter panel before
    getFilteredConfig could throw on selected.length, and tableau has no error boundary. trex
    survived only because its activeSheetId names a dashboard, so the fallback opened the one sheet
    with no filters; clicking "Batch recovery" threw. Separately the seeded uiState shares no key
    with the one this UI reads, so Explore threw on exploreFilter.startsWith.
    """
    node_or_skip()
    patch = next(p for p in hub_app.patches("tableau_mock") if p["file"] == "src/utils/dataManager.js")
    seed = tableau_state(ROW_CHART, filters=[SEEDED_FILTER])
    out = run_node(
        tmp_path,
        "filters.mjs",
        f"const createInitialData = () => ({{ users: [], uiState: {json.dumps(TABLEAU_UI_DEFAULTS)} }});\n"
        + patch["new"]
        + "\n  return customState\n}\n"
        + "const out = initializeData('s', JSON.parse(process.argv[2]));\n"
        + "console.log(JSON.stringify({ sheet: out.workbooks[0].sheets[0], ui: out.uiState, users: out.users }));\n",
        seed,
    )
    [mapped] = out["sheet"]["filters"]
    assert mapped["id"] == "fld-supplier" and mapped["field"] == "Supplier"
    # The seed says what is selected and never what the domain is, so the sheet renders unfiltered.
    assert mapped["selected"] == mapped["values"] == ["Alder Film", "Brook"]
    assert mapped["selectedValues"] == ["Alder Film", "Brook"]  # the seeded shape survives for saves
    assert out["ui"]["exploreFilter"] == "all" and out["ui"]["dashboardFilters"] == {}
    assert out["ui"]["sidebarTab"] == "data"  # the seeded uiState still overlays the defaults
    assert out["ui"]["currentPage"] == "workbook" and out["ui"]["selectedSheet"] == "ws-1"
    # AdminPage lists the site's people with u.siteRole.toLowerCase(); the seed says role.
    assert out["users"] == [{"name": "Ibrahim Sethi", "role": "Creator", "siteRole": "Creator"}]


def test_patched_tableau_adapter_opens_a_sheet_that_exists(tmp_path):
    """trex-company's workbook.activeSheetId names a dashboard, not a worksheet.

    The adapter maps only worksheets into sheets, so uiState.selectedSheet pointed at nothing and
    the tab strip highlighted no tab while the view fell back to sheets[0].
    """
    node_or_skip()
    patch = next(p for p in hub_app.patches("tableau_mock") if p["file"] == "src/utils/dataManager.js")
    seed = tableau_state(ROW_CHART)
    seed["workbook"]["activeSheetId"] = "dash-purchasing"
    out = run_node(
        tmp_path,
        "open.mjs",
        f"const createInitialData = () => ({{ users: [], uiState: {json.dumps(TABLEAU_UI_DEFAULTS)} }});\n"
        + patch["new"]
        + "\n  return customState\n}\n"
        + "const out = initializeData('s', JSON.parse(process.argv[2]));\n"
        + "console.log(JSON.stringify({ ui: out.uiState }));\n",
        seed,
    )
    assert out["ui"]["selectedSheet"] == "ws-1"


def test_the_entry_route_has_one_upstream_source_that_is_not_pinned_provenance():
    """The gate, the VM launcher and the worker's start page must open the same page.

    `catalogs/apps.json` would be the natural column, but `manifest.json` hashes it as imported
    provenance and editing it registers an integrity error -- the same wall the imported schema
    documents hit. So the routes live in a file of ours, and the gate's literals are only a fallback
    for a checkout without it.
    """
    routes = hub_app.entry_routes()
    assert routes["google_sheets_mock"] == "/spreadsheet"
    assert routes["google_drive_mock"] == "/recent"
    assert all(path.startswith("/") for path in routes.values())
    assert "apps.json" not in str(hub_app.ENTRY_ROUTES_PATH)


def test_a_missing_or_malformed_entry_route_file_leaves_every_app_on_its_own_route(tmp_path):
    assert hub_app.entry_routes(tmp_path / "absent.json") == {}
    bad = tmp_path / "routes.json"
    bad.write_text('{"apps": {"a_mock": {"entry_path": "no-leading-slash"}, "b_mock": {}}}')
    assert hub_app.entry_routes(bad) == {}


def test_a_copied_build_inherits_no_claim_on_the_original(tmp_path):
    """A hardlinked or copied build carries the warm cache's serving marker, and HubProcess then
    refuses the private copy as already served by the process holding the cache. Found when a probe
    run hardlinked experiments/hub-cache while something was serving it."""
    source, target = tmp_path / "warm", tmp_path / "private"
    source.mkdir()
    (source / "vite.config.js").write_text("served")
    (source / hub_app.SERVING_MARKER).write_text(str(os.getpid()))
    assert hub_app.serving_pid(source) == os.getpid()
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*hub_app.SOURCE_EXCLUDES))
    assert not (target / hub_app.SERVING_MARKER).exists()
    assert hub_app.serving_pid(target) is None


@pytest.mark.parametrize(
    ("app_id", "state_file"),
    [
        ("quickbooks_mock", "src/lib/initialData.js"),
        ("salesforce_mock", "src/data/initialData.ts"),
        ("workday_mock", "src/lib/mockData.js"),
    ],
)
def test_no_clone_posts_its_state_with_keepalive(tmp_path, app_id, state_file):
    """A keepalive request body is capped at 64 KiB, and these three swallowed the rejection.

    Every fixture is 3-7 KB and every real company world is 199 KB to 526 KB, so the write worked in
    every test and was discarded in every company: measured on one app, same steps and same build,
    a 3,010-byte seed posted 2,783 bytes and answered 200 with the marker in /state, and a
    338,130-byte seed posted 306,788 bytes, got no response at all and wrote nothing. quickbooks has
    3 worlds and salesforce 4, all of them over the cap; workday has none yet and would have failed
    on its first. This is the westlaw_mock shape -- passes every protocol check, throws the work
    away -- so the guard is against the flag, not against one app.
    """
    text = (patched_copy(tmp_path, app_id) / state_file).read_text()
    assert "keepalive" not in text
    assert "body: JSON.stringify({ action: 'set_current', state })" in text, "the POST itself stays"


def test_epic_health_posts_its_state_back_at_all(tmp_path):
    """It was the only clone of 98 with no /post writer, so no UI action was ever gradeable.

    It read its seeded world from /state and then kept every change in localStorage and
    window.__APP_STATE__ only. A write search made 215 real submit attempts and reached the state
    zero times; sending a message from /messages/compose left /state byte-identical.
    """
    text = (patched_copy(tmp_path, "epic-health_mock") / "src/context/AppContext.jsx").read_text()
    assert "localStorage.setItem(sKey, JSON.stringify(state))" in text
    assert "action: 'set_current'" in text and "/post?sid=" in text
    assert "keepalive" not in text, "the defect the other three had is not introduced here"
    assert text.count("window.__hubStateSync") == 2, "debounced once, the way the other clones save"
