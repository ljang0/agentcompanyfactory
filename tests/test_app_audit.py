"""Browser audit decisions use doubles; these tests never launch Chromium or node."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from company_envs.world import app_audit as audit

SCHEMA = """# Example
## State Schema
| Key | Type | Description |
|---|---|---|
| `currentUser` | User | Active user |
| `tickets` | `Ticket[]` | Support tickets |
| `comments` | object | Keyed by ticket ID → array of comments |
| `empty` | array | Records without a fixture |
| `ui` | object | Current panel |
| `selectedItems` | array | Selected IDs |
| `tickets[].labels` | array | Nested field |
### Ticket
| `nested` | array | Not a top-level collection |
## Routes
| Path | Component | Description |
|---|---|---|
| `/` | Home | Home |
| `/tickets/:ticketId` | Ticket | Detail |
| `/missing` | Missing | Separate page |
| `/go` | State | Inspector |
## Reducer Actions
| Action Type | Payload | Effect |
|---|---|---|
| `LOAD_STATE` | state | Startup |
| `ADD_TICKET` | ticket | Create ticket |
| `DELETE_TICKET` | id | Remove ticket |
## Observable State Changes
| User Action | State Field Changed |
|---|---|
| Save draft | `tickets` |
"""
STATE = {
    "currentUser": {},
    "tickets": [{"id": 7, "title": "Broken elevator"}],
    "comments": {"7": [{"id": 9, "content": "Technician booked"}]},
    "empty": [],
    "ui": {},
    "selectedItems": [],
}


def snapshot(text="Tickets", names=("New ticket",), lists=None):
    return {
        "text": text,
        "controls": [{"name": name, "disabled": False} for name in names],
        "lists": lists or {},
    }


class Client:
    def __init__(self):
        self.state = copy.deepcopy(STATE)
        self.base_url = "http://127.0.0.1:8765"

    def current(self, sid):
        return {"stored_state": copy.deepcopy(self.state)}

    def seed(self, sid, state):
        self.state = copy.deepcopy(state)


class UI:
    def __init__(self, pages=None, client=None, save=True, forget=False):
        self.pages = pages or {}
        self.client = client or Client()
        self.errors = []
        self.save = save
        self.forget = forget
        self.edited = False
        self.calls = []

    def fresh(self):
        self.edited = False
        self.calls.append("fresh")

    def navigate(self, route):
        self.route = route
        self.calls.append(route)
        if route == "/crash":
            raise RuntimeError("Page crashed")
        if self.edited and self.forget:
            self.client.state = copy.deepcopy(STATE)

    def snapshot(self, name):
        return copy.deepcopy(self.pages.get(self.route, snapshot("Fallback")))

    def step(self, step):
        self.edited = True
        if self.save:
            self.client.state["tickets"][0]["title"] = step["value"]
        else:
            self.client.state["ui"]["clock"] = 12

    def pause(self):
        pass

    def close(self):
        self.closed = True


def probe():
    return {
        "collection": "tickets",
        "marker": "Saved through UI",
        "steps": [{"op": "fill", "value": "Saved through UI"}],
    }


def test_inventory_reads_full_schema_typed_arrays_keyed_maps_and_action_variants():
    inventory = audit.schema_inventory(SCHEMA)
    assert set(inventory["collections"]) == {"tickets", "comments", "empty"}
    assert [r["route"] for r in inventory["routes"]] == ["/", "/tickets/:ticketId", "/missing"]
    assert [a["action"] for a in inventory["actions"]] == ["ADD_TICKET", "DELETE_TICKET", "Save draft"]
    assert inventory["internal_actions"][0]["action"] == "LOAD_STATE"
    assert all(row["line"] > 0 for row in inventory["actions"])


def test_inventory_handles_application_routes_subsections_and_escaped_pipes():
    text = SCHEMA.replace("## Routes", "## Application Routes").replace(
        "## Reducer Actions", "## Actions / State Mutations\n### Ticket Actions"
    )
    text = text.replace("Ticket[]", "array\\|null")
    inventory = audit.schema_inventory(text)
    assert "tickets" in inventory["collections"]
    assert len(inventory["routes"]) == 3
    assert len(inventory["actions"]) == 3


@pytest.mark.parametrize(
    "route,expected",
    [("/tickets/:ticketId", "/tickets/7"), ("/tickets/:id", "/tickets/7"), ("/*", "/audit-unmatched-route")],
)
def test_route_parameters_come_from_seed(route, expected):
    assert audit.resolve_route(route, STATE) == expected


def test_routes_use_project_keys_thread_ids_and_entity_specific_ids():
    state = {
        "projects": [{"id": "p1", "key": "TEST"}],
        "emails": [{"id": "e1", "threadId": "t1"}],
        "leads": [{"leadId": "l1"}],
        "opportunities": [{"opportunityId": "o1"}],
        "reportCategories": [{"reports": [{"id": "income"}]}],
    }
    assert audit.resolve_route("/project/:key/board", state) == "/project/TEST/board"
    assert audit.resolve_route("/#/email/:threadId", state) == "/#/email/t1"
    assert audit.resolve_route("/leads/:id", state) == "/leads/l1"
    assert audit.resolve_route("/opportunities/:id", state) == "/opportunities/o1"
    assert audit.resolve_route("/reports/:reportId", state) == "/reports/income"
    with pytest.raises(ValueError, match="No seeded value"):
        audit.resolve_route("/unknown/:id", STATE)


def test_session_url_preserves_hash_and_existing_query_and_rejects_external_routes():
    assert (
        audit.session_url("http://local", "/?create=1#/inbox", "fresh")
        == "http://local/?create=1&sid=fresh#/inbox"
    )
    assert audit.session_url("http://local", "/?sid=old", "fresh") == "http://local/?sid=fresh"
    for route in ("https://example.com/", "//example.com/", "relative"):
        with pytest.raises(ValueError, match="local app"):
            audit.session_url("http://local", route, "fresh")


def test_record_labels_flatten_maps_without_using_foreign_keys_or_status():
    assert audit.record_labels(STATE["comments"]) == ["Technician booked"]
    assert audit.record_labels({"u": {"firstName": "Ada", "lastName": "Young", "role": "Admin"}}) == [
        "Ada Young"
    ]
    assert audit.record_labels([{"id": 8, "ownerId": 5, "status": "Open"}]) == []
    assert audit.record_labels([{"content": "<p>Visible paragraph</p>"}]) == ["Visible paragraph"]


def test_audit_requires_record_rows_exact_controls_and_distinct_route_content():
    client = Client()
    pages = {
        "/": snapshot("Broken elevator appears only in header", names=("Create",)),
        "/tickets/7": snapshot(
            "Ticket detail", lists={"tickets": ["7 Broken elevator Open"], "comments": ["Technician booked"]}
        ),
    }
    report = audit.audit_session(
        UI(pages, client), client, "s", STATE, audit.schema_inventory(SCHEMA), {"write": probe()}
    )
    assert report["renders"]
    assert report["collections_visible"]["tickets"]["ok"]
    assert report["collections_visible"]["comments"]["ok"]
    assert not report["collections_visible"]["empty"]["ok"]
    assert "empty" in report["collections_visible"]["empty"]["gap"]
    assert [r["ok"] for r in report["routes_ok"]] == [True, True, False]
    assert [a["ok"] for a in report["actions_found"]] == [True, False, False]
    assert report["write_roundtrip"]["ok"]


def test_api_and_body_text_alone_cannot_pass_collections_or_disabled_actions():
    page = snapshot(json.dumps(STATE), names=("Delete ticket",))
    page["controls"][0]["disabled"] = True
    ui = UI({"/": page, "/tickets/7": page, "/missing": page})
    report = audit.audit_session(ui, ui.client, "s", STATE, audit.schema_inventory(SCHEMA), {})
    assert not any(c["ok"] for c in report["collections_visible"].values())
    assert not any(c["ok"] for c in report["actions_found"])
    assert not report["write_roundtrip"]["ok"]


def test_scoped_action_alias_and_collection_view_never_match_other_views():
    ui = UI({"/": snapshot(names=("Create",), lists={"tickets": ["Broken elevator"]})})
    plan = {
        "collection_views": {"tickets": ["detail"]},
        "action_controls": {
            "ADD_TICKET": [{"view": "home", "name": "Create"}],
            "DELETE_TICKET": [{"view": "detail", "name": "Create"}],
        },
    }
    report = audit.audit_session(ui, ui.client, "s", STATE, audit.schema_inventory(SCHEMA), plan)
    assert not report["collections_visible"]["tickets"]["ok"]
    assert report["actions_found"][0]["ok"]
    assert not report["actions_found"][1]["ok"]


def test_crashed_and_blank_views_are_reported_and_do_not_stop_remaining_checks():
    ui = UI({"/": snapshot("", names=())})
    report = audit.audit_session(
        ui,
        ui.client,
        "s",
        STATE,
        audit.schema_inventory(SCHEMA),
        {"views": [{"name": "crashed", "route": "/crash"}]},
    )
    assert not report["renders"]
    assert report["views"]["crashed"]["gap"] == "Page crashed"
    assert len(report["routes_ok"]) == 3


def test_console_error_prevents_clean_render():
    class ErrorUI(UI):
        def navigate(self, route):
            super().navigate(route)
            self.errors.append({"kind": "javascript", "message": "ReferenceError"})

    ui = ErrorUI()
    report = audit.audit_session(ui, ui.client, "s", STATE, audit.schema_inventory(SCHEMA), {})
    assert not report["renders"]
    assert report["console_errors"]


@pytest.mark.parametrize("save,forget,ok", [(True, False, True), (False, False, False), (True, True, False)])
def test_write_checks_expected_collection_value_and_reload(save, forget, ok):
    ui = UI(save=save, forget=forget)
    result = audit.write_probe(ui, ui.client, "s", probe())
    assert result["ok"] == ok
    assert result["before"] == STATE["tickets"]
    if ok:
        assert result["changed_keys"] == ["tickets"]
        assert result["after"] == result["after_reload"]
    else:
        assert result["gap"]


def test_write_rejects_marker_already_in_state_and_handles_failed_control():
    ui = UI()
    ui.client.state["tickets"][0]["title"] = probe()["marker"]
    assert "already exists" in audit.write_probe(ui, ui.client, "s", probe())["gap"]
    ui = UI()
    ui.step = lambda _: (_ for _ in ()).throw(RuntimeError("Missing Save control"))
    assert "Missing Save" in audit.write_probe(ui, ui.client, "s", probe())["gap"]


def test_run_uses_hub_process_seeds_and_closes_browser_and_server(tmp_path, monkeypatch):
    client = Client()
    events = []

    class Process:
        def __init__(self, built):
            self.client = client

        def __enter__(self):
            events.append("start")
            return self

        def __exit__(self, *args):
            events.append("stop")

    ui = UI(client=client)
    monkeypatch.setattr(audit, "HubProcess", Process)
    monkeypatch.setattr(audit, "BrowserUI", lambda *args: ui)
    schema = tmp_path / "SCHEMA.md"
    schema.write_text(SCHEMA)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(STATE))
    report = audit.audit_app("demo", tmp_path, schema, state, tmp_path / "out", None, {"write": probe()})
    assert events == ["start", "stop"] and ui.closed
    assert report["api_seed_readback"]
    assert len(report["schema_sha256"]) == 64
    assert json.loads((tmp_path / "out/demo.json").read_text())["app"] == "demo"
    audit.write_summary([report], tmp_path / "out")
    assert "demo" in (tmp_path / "out/SUMMARY.md").read_text()
    state.write_text(json.dumps({k: v for k, v in STATE.items() if k != "empty"}))
    drift = audit.audit_app("demo", tmp_path, schema, state, tmp_path / "drift", None, {})
    assert any(gap["check"] == "seed_schema" for gap in drift["gaps"])
    assert "home" in drift["views"]
    monkeypatch.setattr(audit, "audit_session", lambda *args: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(RuntimeError, match="failure"):
        audit.audit_app("demo", tmp_path, schema, state, tmp_path / "out", None, {})
    assert events == ["start", "stop"] * 3 and ui.closed


def test_browser_wrapper_uses_browser_input_and_closes_context(tmp_path):
    calls = []

    class Target:
        def __getattr__(self, name):
            return lambda *a, **kw: calls.append((name, a, kw)) or self

    class Page(Target):
        url = "http://local"
        keyboard = Target()

        def evaluate(self, script, selectors):
            assert "localStorage" not in script and "fetch(" not in script
            return snapshot()

    page = Page()
    context = SimpleNamespace(new_page=lambda: page, close=lambda: calls.append("close"))
    browser = SimpleNamespace(new_context=lambda **kw: context)
    ui = audit.BrowserUI(browser, "http://local", "sid", tmp_path, {})
    # goto must look like Playwright's response object.
    page.goto = lambda *a, **kw: SimpleNamespace(status=200)
    ui.navigate("/")
    for step in [
        {"op": "click", "role": "button", "name": "Save"},
        {"op": "fill", "placeholder": "Name", "value": "Ada"},
        {"op": "dblclick", "text": "Record"},
        {"op": "select_option", "selector": "select", "index": 0, "value": "a"},
        {"op": "press", "value": "Enter"},
    ]:
        ui.step(step)
    assert ui.snapshot("home")["screenshot"] == "home.png"
    ui.close()
    assert "close" in calls
    assert any(isinstance(call, tuple) and call[0] == "fill" for call in calls)


def test_script_default_bundle_has_all_twelve_apps():
    path = Path(__file__).parents[1] / "scripts/audit_apps.py"
    spec = importlib.util.spec_from_file_location("audit_apps_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.DEFAULT_APPS) == 12
    assert {"clio_mock", "Zendesk_mock", "google_sheets_mock"} <= set(module.DEFAULT_APPS)


def test_each_view_discards_prior_ui_changes_before_opening_the_seed():
    class EditingUI(UI):
        def snapshot(self, name):
            assert self.client.state["tickets"] == STATE["tickets"]
            self.client.state["tickets"][0]["title"] = "Changed by the previous view"
            return snapshot()

    ui = EditingUI()
    audit.audit_session(ui, ui.client, "s", STATE, audit.schema_inventory(SCHEMA), {})
    assert ui.calls.count("fresh") == 6
    assert ui.client.state == STATE


def test_home_alias_is_not_rejected_for_matching_the_unknown_route_fallback():
    home = snapshot("Inbox")
    home["url"] = "http://local/?sid=s#/inbox"
    ui = UI({"/": home, "/#/inbox": home})
    inv = audit.schema_inventory(SCHEMA)
    inv["routes"] = [{"route": "/#/inbox", "description": "Inbox", "line": 1}]
    report = audit.audit_session(ui, ui.client, "s", STATE, inv, {})
    assert report["routes_ok"][0]["ok"]


def test_action_names_do_not_credit_generic_create_or_partial_matches():
    names = audit.action_names("Create a new ticket (via dialog)")
    assert names == {"create ticket", "add ticket", "new ticket"}
    assert "create" not in names and "create ticket template" not in names
    assert "edit ticket" in audit.action_names("UPDATE_TICKET")


def test_schema_keeps_history_records_and_does_not_read_nested_ui_arrays():
    text = """## State Schema
| Key | Type | Description |
|---|---|---|
| `ui` | object | Navigation |
### UI fields
| `panels` | array | Not records |
## State Schema
| `callHistory` | array | Calls |
| `notifications` | array | Notifications |
| `invitations` | array | Invitations |
"""
    assert set(audit.schema_inventory(text)["collections"]) == {"callHistory", "notifications", "invitations"}
    assert audit.record_labels([{"fullName": "Ada Smith", "title": "Director"}]) == ["Ada Smith"]


def test_failed_step_keeps_the_screen_and_does_not_claim_empty_ui():
    ui = UI({"/": snapshot("Form remains visible")})
    ui.step = lambda _: (_ for _ in ()).throw(RuntimeError("Save is missing"))
    result = audit.write_probe(ui, ui.client, "s", probe())
    assert not result["ok"]
    assert result["failure_view"]["text"] == "Form remains visible"
    assert result["after"] == STATE["tickets"]


def test_script_saves_startup_failure_continues_and_closes_browser(tmp_path, monkeypatch):
    import sys

    path = Path(__file__).parents[1] / "scripts/audit_apps.py"
    spec = importlib.util.spec_from_file_location("audit_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = []
    browser = SimpleNamespace(close=lambda: events.append("closed"))

    class Playwright:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(sync_playwright=Playwright))

    def run(app, *args):
        events.append(app)
        if app == "bad":
            raise RuntimeError("Missing dist")
        return {
            "app": app,
            "renders": True,
            "collections_visible": {},
            "routes_ok": [],
            "actions_found": [],
            "write_roundtrip": {"ok": True},
            "gaps": [],
        }

    monkeypatch.setattr(module, "audit_app", run)
    probes = tmp_path / "probes.json"
    probes.write_text("{}")
    output = tmp_path / "result"
    assert module.main(["bad", "good", "--probes", str(probes), "--output", str(output)]) == 1
    assert events == ["bad", "good", "closed"]
    assert json.loads((output / "bad.json").read_text())["error"] == "Missing dist"
    assert "good" in (output / "SUMMARY.md").read_text()
    with pytest.raises(FileExistsError):
        module.main(["good", "--probes", str(probes), "--output", str(output)])


def test_a_write_recipe_is_derived_from_the_app_source_when_no_plan_supplies_one(tmp_path):
    """ "No UI write recipe supplied" is the gap that keeps 76 apps ungradeable.

    Only 12 of 88 apps have ever demonstrated that a UI action reaches the state a grader reads, and
    those 12 are exactly the apps with a hand-written plan. A generic probe improved to open a
    creation form first found one on one of eight apps and landed a write on none, so the plans are
    the answer -- but three of the four facts a plan needs are in the app's own source: the
    collection its reducer appends to, the field the creation form asks for, and the button that
    submits it. 75 of 88 apps yield an append into a documented collection this way.
    """
    source = tmp_path / "src"
    (source / "context").mkdir(parents=True)
    (source / "components").mkdir()
    (source / "context/AppContext.jsx").write_text(
        """
        function reducer(state, action) {
          switch (action.type) {
            case 'CREATE_TICKET':
              return {...state, tickets: [...state.tickets, action.ticket]};
            case 'ADD_COMMENT':
              return {...state, comments: {...state.comments, [action.id]: [...state.comments[action.id], action.comment]}};
            case 'SELECT':
              return {...state, selectedItems: [action.id]};
          }
        }
        """
    )
    (source / "components/NewTicket.jsx").write_text(
        """
        export default function NewTicket() {
          const {dispatch} = useApp();
          function submit() { dispatch({type: 'CREATE_TICKET', ticket: {id: 1}}); }
          return (<div>
            <input placeholder="Ticket subject" />
            <textarea placeholder="Describe the problem" />
            <button onClick={submit}>Create ticket</button>
            <button onClick={close}>Cancel</button>
          </div>);
        }
        """
    )
    hints = audit.derive_write_hints(source, ["tickets", "comments", "selectedItems"])
    assert set(hints) == {"tickets", "comments"}, "a selection array is not a record collection"
    assert hints["tickets"]["actions"] == ["CREATE_TICKET", "reducer"]
    assert hints["tickets"]["forms"] == ["components/NewTicket.jsx"]
    assert hints["tickets"]["placeholders"] == ["Ticket subject", "Describe the problem"]
    assert hints["tickets"]["buttons"] == ["Create ticket", "Cancel"]
    assert hints["comments"]["writers"] == ["context/AppContext.jsx"]
    # A keyed map of message lists is a collection too: slack's messages live under a channel id.
    assert hints["comments"]["actions"] == ["ADD_COMMENT", "reducer"]
    assert audit.derive_write_hints(tmp_path / "missing") == {}


def test_an_append_is_found_in_each_idiom_the_hub_apps_write_it(tmp_path):
    """Reading only the object-literal spread makes 8 of 88 apps look as if they never write.

    The first search for write probes derived the collection from two idioms, the object-literal
    spread and the keyed index, and reported "No append into a contract collection found in the
    app source" for 13 apps -- which became the list of apps needing a hand-written plan. Eight of
    those 13 do append, in an idiom the two patterns cannot see: aws_console assigns
    (``newState.ec2 = [...prev.ec2, instance]``), Expensify names a local first, google_drive
    inserts into a record map by key, klaviyo pushes, zoom_web passes a new array to a hook's
    setter, and robinhood appends to a copy under a local name. With all eight idioms the apps that
    yield an append into a documented collection go from 76 of 88 to 84 of 88.
    """
    source = tmp_path / "src"
    source.mkdir(parents=True)
    (source / "store.jsx").write_text(
        """
        function reducer(prev, action) {
          const newState = {...prev};
          switch (action.type) {
            case 'LAUNCH_INSTANCE':
              newState.ec2 = [...prev.ec2, action.payload];
              break;
            case 'ADD_EXPENSE': {
              const expenses = [...prev.expenses, action.payload];
              return {...prev, expenses};
            }
            case 'CREATE_FOLDER':
              return {...prev, items: {...prev.items, [action.folder.id]: action.folder}};
            case 'SELECT':
              return {...prev, selectedItems: [action.id]};
          }
        }
        const placeOrder = (order) => setState(prev => {
          const newTransactions = [...prev.transactions];
          newTransactions.push(order);
          return {...prev, transactions: newTransactions};
        });
        const importProfile = (profile) => { draft.profiles.push(profile); };
        const scheduleMeeting = (meeting) => setMeetings([...meetings, meeting]);
        """
    )
    hints = audit.derive_write_hints(
        source, ["ec2", "expenses", "items", "transactions", "profiles", "meetings", "selectedItems"]
    )
    assert {key: value["idioms"] for key, value in hints.items()} == {
        "ec2": ["assign_spread"],
        "expenses": ["assign_spread"],
        "items": ["map_insert"],
        "transactions": ["aliased"],
        "profiles": ["mutated"],
        "meetings": ["setter"],
    }, "a selection array is replaced, not appended to, and is not a write"
    assert hints["ec2"]["actions"] == ["LAUNCH_INSTANCE", "reducer"]
    # The enclosing name is the nearest case or function above the append, so a setter outside a
    # reducer also picks up the file's last case label; the component search uses both names.
    assert "scheduleMeeting" in hints["meetings"]["actions"]
    # The alias has to be grown or assigned back; copying a collection alone is not an append.
    copy_only = tmp_path / "copy"
    (copy_only / "src").mkdir(parents=True)
    (copy_only / "src/view.jsx").write_text("const rows = [...state.invoices]; rows.sort();")
    assert audit.derive_write_hints(copy_only / "src", ["invoices"]) == {}


def test_every_stored_write_plan_is_a_shape_the_browser_wrapper_can_replay():
    """A plan the step vocabulary cannot express is a write probe that silently never runs.

    probes.json is the only record of which apps have ever proved that a UI action reaches the
    state a grader reads, and a plan is only worth anything if BrowserUI.step can address every
    one of its steps. Six apps were found to write and then lost because the recipe could not be
    said in this vocabulary: an unnamed textbox has no placeholder to match and a link Playwright
    scores as `generic` cannot be reached by role, so a step also has to be expressible as a
    position inside a CSS group. The twelve hand-written plans are the ones marked "Audit saved
    value 2026"; they are the project's original write evidence and must stay as they are.

    The file holds 72 plans, 71 of which replay from the smoke seed with the marker surviving a
    reload (experiments/app-audit/derived-20260910/RESULTS-pass2.json). Zendesk's is the one that
    does not: its new-ticket flow has been broken since the 2026-09-08 audit.
    """
    plans = json.loads((Path(__file__).parents[1] / "experiments/app-audit/probes.json").read_text())
    writes = {app: value["write"] for app, value in plans.items() if value.get("write")}
    assert len(writes) >= 72, "a pass that removes plans is a regression, not a repair"
    hand_written = sorted(app for app, write in writes.items() if write["marker"] == "Audit saved value 2026")
    assert hand_written == [
        "Zendesk_mock",
        "clio_mock",
        "gmail_mock",
        "google_calendar_mock",
        "google_docs_mock",
        "google_drive_mock",
        "google_sheets_mock",
        "hubspot_mock",
        "jira_mock",
        "quickbooks_mock",
        "salesforce_mock",
        "slack_mock",
    ]
    for app, write in sorted(writes.items()):
        assert set(write) <= {"collection", "marker", "route", "prepare", "steps"}, app
        assert write["collection"] and write["marker"], app
        assert write.get("route", "/").startswith("/"), app
        steps = write.get("prepare", []) + write["steps"]
        assert any(step["op"] in {"fill", "select_option"} for step in steps), f"{app} types nothing"
        for step in steps:
            assert step["op"] in {"click", "dblclick", "hover", "fill", "press", "select_option"}, app
            if step["op"] in {"fill", "press", "select_option"}:
                assert "value" in step, f"{app}: {step}"
            addressed = {"selector", "role", "placeholder", "text"} & set(step)
            assert addressed or step["op"] == "press", f"{app}: {step} addresses no element"
            if "role" in step and step["op"] == "click":
                assert step.get("name"), f"{app}: a role without a name matches every control"
    # airtable's plan fills get_by_role("textbox", name="") and only replays because that view has
    # exactly one textbox. The search now says an unnamed field by position instead, which is why a
    # positional step has to be part of the vocabulary rather than a fallback nobody can express.
    airtable = writes["airtable_mock"]["steps"][0]
    assert airtable["op"] == "fill" and airtable.get("name") == ""
