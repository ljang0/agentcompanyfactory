"""The click-through checker's own rules: what counts as an error, and how volume is built."""

import json

from company_envs.world.interact_check import (
    ERROR_TEXT,
    SKIP_WORDS,
    amplify_state,
    error_near,
    readable,
    sample_strings,
    screen_error,
)


def test_a_business_word_containing_nan_is_not_an_error():
    """ "Finance" contains "nan"; a case-insensitive NaN failed every screen that showed a label."""
    assert error_near("Inbox Finance Primary Social Promotions") is None
    assert error_near("Total owed NaN") is not None
    assert ERROR_TEXT.search("Channel not found")
    assert ERROR_TEXT.search("[object Object]")


def test_a_screen_error_is_reported_with_enough_text_to_read():
    found = error_near("Opened the board and then Something went wrong while loading the cards")
    assert found is not None and "Something went wrong" in found
    assert "board" in found  # the words around it, so a person can tell what broke


def test_controls_that_would_end_the_session_are_never_clicked():
    for label in ("Sign out", "Log out", "Delete account", "Reset", "Clear all"):
        assert SKIP_WORDS.search(label), label
    for label in ("Send", "Save draft", "Create issue", "Inbox", "Settings"):
        assert not SKIP_WORDS.search(label), label


def test_amplify_grows_a_fixture_to_real_volume_under_fresh_identities():
    state = {
        "emails": [{"id": "m1", "subject": "Quarter close", "threadId": "t1"}],
        "users": {"u1": {"id": "u1", "name": "Ada Iyer"}},
        "settings": {"theme": "light"},
    }
    grown = amplify_state(state, 40_000)
    assert len(json.dumps(grown)) >= 40_000
    assert len(grown["emails"]) > len(state["emails"])
    assert len({record["id"] for record in grown["emails"]}) == len(grown["emails"])  # ids stay unique
    assert len({key for key in grown["users"]}) == len(grown["users"])
    assert grown["settings"] == {"theme": "light"}  # a settings object is not a collection to grow


def test_amplify_stops_instead_of_spinning_when_there_is_nothing_to_grow():
    state = {"ui": {"view": "week"}, "version": 3}
    grown = amplify_state(state, 1_000_000)
    assert grown == state


def test_sample_strings_picks_readable_values_a_screen_would_show():
    state = {
        "emails": [{"subject": "Harbor Airport numbers", "id": "m1", "url": "https://example.test/x"}],
        "count": 7,
    }
    found = sample_strings(state)
    assert "Harbor Airport numbers" in found
    assert not any(value.startswith("http") for value in found)  # a link is not readable content
    assert "m1" not in found  # an identifier is not readable content


def test_a_value_with_no_space_in_it_still_reads_to_a_person():
    """Requiring a space skipped every value in a Chinese-language app, so dingtalk and greenhouse
    were reported as not rendering their seeded records while a screenshot shows they plainly do."""
    assert readable("清风冷链调度群")
    assert readable("Procurement")
    assert not readable("a1b2c3d4e5")  # an identifier
    assert not readable("m1")
    assert not readable("https://example.test/logo.png")


def test_sampling_spreads_across_collections_instead_of_draining_the_first():
    """Depth-first filled the whole sample from one buried collection, so a screen showing nine
    other collections' records read as showing none of them."""
    state = {
        "messages": [{"body": f"Quarterly note number {n}"} for n in range(200)],
        "invoices": [{"memo": "Harbor dredging retainer"}],
        "people": [{"name": "Nadine Escobar"}],
    }
    found = sample_strings(state, limit=12)
    assert "Harbor dredging retainer" in found
    assert "Nadine Escobar" in found


def test_a_search_box_is_the_last_place_a_write_probe_types():
    """Typing into a filter changes nothing, and it was the first box on the page in several apps --
    gmail's "Search mail" stood in for a write, and the diff that followed came from elsewhere."""
    from company_envs.world.interact_check import SEARCH_WORDS

    for label in ("Search mail", "Filter tickets", "Find a contact", "Jump to", "Search"):
        assert SEARCH_WORDS.search(label), label
    for label in ("Subject", "Write a message", "Describe the issue", "Card title"):
        assert not SEARCH_WORDS.search(label), label


def test_the_probe_opens_a_form_before_looking_for_somewhere_to_type():
    """Without this the probe types into whatever box the page already shows, which is a filter on
    most apps -- only 12 of 88 apps had ever demonstrated a write reaching grader-visible state."""
    from company_envs.world.interact_check import CREATE_WORDS

    for label in ("New", "Create issue", "Compose", "Add task", "+"):
        assert CREATE_WORDS.match(label), label
    for label in ("Renew subscription", "Addresses", "Created by", "Newsletter"):
        assert not CREATE_WORDS.match(label), label


def test_an_apps_own_records_are_not_read_as_a_broken_screen():
    """sentry_mock seeds 2,048 issue titles that really are error strings, so scanning the whole
    page reported the app broken when it had passed every other check, in a guest included. A broken
    screen says so in its headline, or it is too sparse to be showing records at all."""
    from company_envs.world.interact_check import HEADLINE

    # A dense page whose failure phrases all sit past the headline is showing records.
    chrome = "Issues  Unresolved  Assigned  For review  Saved searches  " + "x" * HEADLINE
    titles = "  ".join(
        f"TypeError: Cannot read properties of undefined (reading 'stopId') SENTRY-{n}" for n in range(200)
    )
    page = chrome + titles
    assert len(page) > HEADLINE
    assert error_near(page) is None, "the app's own issue titles are its records, not a fault"


def test_a_sparse_screen_showing_a_failure_phrase_is_still_broken():
    """A failure phrase beside no content is exactly what a blank, broken app looks like --
    outlook_web renders 0 characters, monday says "Board not found" and nothing else."""
    assert error_near("Board not found") is not None
    assert error_near("Something went wrong. Try again later.") is not None
    # And in the headline of a page that does have content.
    assert error_near("Inbox  Channel not found  " + "y" * 5000) is not None


def test_an_app_whose_records_are_error_strings_is_exempt_by_name():
    """sentry_mock seeds 2,048 issue titles like "TypeError: Cannot read properties of undefined",
    and its very first row is one -- so no positional window separates its records from a fault. It
    passed every other check in a live guest, so the rule is switched off for it by name rather than
    loosened for every app.
    """
    titles = "SENTRY-1 TypeError: Cannot read properties of undefined (reading 'stopId') 2.9k 1.9k"
    assert error_near(titles) is not None, "the phrase really is there"
    assert screen_error("sentry_mock", titles) is None
    assert screen_error("gmail_mock", titles) is not None, "every other app is still judged"


class Screen:
    """One screen of a fake app: the controls on it, and where each one leads."""

    def __init__(self, name, controls, routes=None, url=None):
        self.name, self.controls, self.routes, self.url = name, controls, routes or {}, url


class FakePage:
    """Enough of a Playwright page to drive ``click_pass`` without a browser.

    A control that is not on the screen the page is currently showing raises the same timeout a
    real click raises, which is exactly what happened to every target listed on a landing page
    after the first click routed away from it.
    """

    def __init__(self, screens, start, url):
        self.screens, self.here, self.url, self.home = screens, start, url, url
        self.gotos = []

    def screen(self):
        return self.screens[self.here]

    def evaluate(self, script):
        return f"{self.here}|{len(self.screen().controls)}|{' '.join(self.screen().controls)}"

    def wait_for_timeout(self, _ms):
        return None

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url, self.here = url, "home" if url == self.home else self.here

    def get_by_role(self, _role, name=None, exact=False):
        """The shape Playwright has: role positional, name and exact by keyword."""
        del _role, exact
        return FakeLocator(self, name)


class FakeLocator:
    def __init__(self, page, name):
        self.page, self.name = page, name

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        screen = self.page.screen()
        if self.name not in screen.controls:
            raise TimeoutError(
                f"Locator.click: Timeout {timeout}ms exceeded.\nCall log:\n"
                f'  - waiting for get_by_role("link").filter(hasText="{self.name}").first\n'
            )
        if self.name in screen.routes:
            self.page.here = screen.routes[self.name]
            self.page.url = self.page.screens[self.page.here].url or self.page.url


def a_page_whose_landing_screen_is_not_a_nav_shell():
    return FakePage(
        {
            "home": Screen(
                "home",
                ["Spaces", "Quality Assurance", "Health and Safety"],
                routes={"Quality Assurance": "space", "Health and Safety": "space"},
            ),
            "space": Screen("space", ["Back to spaces"], url="http://app.test/?sid=x#/space/1"),
        },
        "home",
        "http://app.test/?sid=x",
    )


def test_a_click_that_routes_inside_the_app_does_not_take_the_next_target_with_it():
    """The clicker only ever came back after a click that left the origin, so an in-app route
    change left every later target on a screen it was not listed on. Measured on the recorded
    88-app run: airtable_mock 1 of 18 controls live and 17 unclickable, and it is not a broken
    app -- with the screen restored the same run is 17 of 18 live and none unclickable."""
    from company_envs.world.interact_check import click_pass

    page = a_page_whose_landing_screen_is_not_a_nav_shell()
    targets = [{"role": "link", "name": name} for name in page.screen().controls]
    rows = click_pass(page, page.home, targets, [], "confluence_mock")

    assert [row["clicked"] for row in rows] == [True, True, True], rows
    assert [row["error"] for row in rows] == [None, None, None]
    assert rows[2]["restored"] is True, "the screen the targets were listed on is put back"
    assert rows[1]["routed_to"].endswith("#/space/1"), "and the route the click opened is recorded"
    assert page.gotos == [page.home], "only the click that routed away costs a reload"


def test_a_dead_control_is_still_dead_once_the_screen_is_restored():
    """Restoring the screen must not turn a control that does nothing into one that works: the
    point of the fix is a verdict that can be trusted in both directions."""
    from company_envs.world.interact_check import click_pass

    page = FakePage({"home": Screen("home", ["Inbox", "Sent"])}, "home", "http://app.test/?sid=x")
    rows = click_pass(page, page.home, [{"role": "link", "name": "Inbox"}], [], "gmail_mock")
    assert rows[0]["clicked"] is True
    assert rows[0]["changed"] is False, "nothing on screen changed, so the click did nothing"
    assert page.gotos == [], "a screen that never changed is never reloaded"


def test_a_click_that_could_not_land_says_why_it_could_not():
    """ "Timeout 2500ms exceeded" is the same sentence whether the control is missing or an
    overlay is covering it -- and 71 of 88 apps recorded at least one of those timeouts with no
    way to tell which. Playwright says which in its call log, which was being thrown away."""
    from company_envs.world.interact_check import click_failure

    covered = TimeoutError(
        "Locator.click: Timeout 2500ms exceeded.\n"
        "Call log:\n"
        '  - waiting for get_by_role("button").first\n'
        "  -   locator resolved to <button>New</button>\n"
        "  - attempting click action\n"
        '  -   <div class="modal-backdrop"></div> intercepts pointer events\n'
        "  - retrying click action\n"
        "  -   waiting 500ms\n"
    )
    said = click_failure(covered)
    assert "intercepts pointer events" in said, "and not the retry bookkeeping printed after it"
    assert "Timeout 2500ms exceeded" in said
    missing = TimeoutError('Locator.click: Timeout 2500ms exceeded.\nCall log:\n  - waiting for locator("x")')
    assert click_failure(missing).endswith("Timeout 2500ms exceeded.")


def test_amplify_reaches_a_collection_nested_inside_another_object():
    """canvas_mock's only collection is ``canvasJSON.objects``, and a top-level-only search grew
    nothing: ``--target-mb 3`` left the state at 1,764 bytes and the run still filed a volume
    verdict. It is the one app of 93 shaped this way, which is why nobody noticed."""
    from company_envs.world.interact_check import amplify

    state = {"canvasJSON": {"version": "6.9.0", "objects": [{"id": "title", "text": "Autumn Market"}]}}
    grown, reached = amplify(state, 60_000)
    assert reached["reached_target"] and reached["grew"]
    assert len(json.dumps(grown)) >= 60_000
    objects = grown["canvasJSON"]["objects"]
    assert len({record["id"] for record in objects}) == len(objects), "ids stay unique"
    assert grown["canvasJSON"]["version"] == "6.9.0", "a field of the object is not a collection"


def test_a_record_is_not_ransacked_for_collections_of_its_own():
    """The lists inside a record are that record's fields. Multiplying them would grow the state
    without growing the number of records, which is not what a volume test asks."""
    from company_envs.world.interact_check import collections_to_grow

    state = {"emails": [{"id": "m1", "to": ["ada@example.test", "ben@example.test"]}]}
    found = collections_to_grow(state)
    assert len(found) == 1 and found[0] is state["emails"]


def test_amplify_says_when_a_state_could_not_be_grown_at_all():
    """A state with no collection in it cannot be volume tested, and the run must be able to say
    so: canvas_mock filed a `mode: volume` report over 1,764 bytes against a 3 MB target."""
    from company_envs.world.interact_check import amplify

    grown, reached = amplify({"ui": {"view": "week"}, "version": 3}, 3_145_728)
    assert grown == {"ui": {"view": "week"}, "version": 3}
    assert reached["grew"] is False and reached["reached_target"] is False
    assert reached["bytes"] == reached["start_bytes"]


class PlanPage:
    """Enough of a Playwright page to replay a probe plan without a browser.

    Fills land in ``typed``; the state the app would keep is only updated when the submit control
    is clicked, so a plan that never reaches its submit cannot pass.
    """

    def __init__(self, submit="Create", store=None):
        self.submit, self.store = submit, store if store is not None else {}
        self.typed, self.gotos, self.clicked = {}, [], []

    def goto(self, url, **_kwargs):
        self.gotos.append(url)

    def wait_for_timeout(self, _ms):
        return None

    def get_by_role(self, _role, name=None, exact=False):
        del _role, exact
        return PlanLocator(self, name)

    def get_by_placeholder(self, placeholder, exact=False):
        del exact
        return PlanLocator(self, placeholder)


class PlanLocator:
    def __init__(self, page, name):
        self.page, self.name = page, name

    def click(self, timeout=None):
        del timeout
        self.page.clicked.append(self.name)
        if self.name == self.page.submit:
            self.page.store.setdefault("tickets", []).append({"subject": self.page.typed.get("Subject")})

    def fill(self, value):
        self.page.typed[self.name] = value


A_PLAN = {
    "collection": "tickets",
    "marker": "Probe saved value 2026",
    "route": "/tickets/new",
    "prepare": [{"op": "click", "role": "button", "name": "Add"}],
    "steps": [
        {"op": "fill", "placeholder": "Subject", "value": "Probe saved value 2026"},
        {"op": "click", "role": "button", "name": "Create"},
    ],
}


def test_a_recipe_is_replayed_on_its_own_route_and_read_back_only_after_a_reload():
    """The generic search reached grader-visible state on 20 of 88 apps where the hand-authored
    recipes reach it on 75, so a report that consults only the search understates the catalogue by
    55 apps. The recipe's own bar is kept: its route, then a reload before the state is read."""
    from company_envs.world.interact_check import plan_probe

    page = PlanPage()
    report = plan_probe(page, "http://app.test", "s1", A_PLAN)
    assert page.gotos[0] == "http://app.test/tickets/new?sid=s1"
    assert page.clicked[0] == "Add", "the prepare step runs before the recipe's own steps"
    assert page.typed["Subject"] == "Probe saved value 2026"
    assert report["from_plan"] and report["attempted"] and report["submitted"] == "Create"
    assert len(page.gotos) == 2, "the state is read back only after the page has been reloaded"
    assert page.store["tickets"], "the fake app kept the record the recipe filed"


def test_a_recipe_that_no_longer_fits_its_app_says_so_instead_of_raising():
    """trello_mock's stored recipe opened "Create new board" on ``/``, and tonight's landing patch
    routes ``/`` to the first board -- one of two stale plans in 77, found by replaying them."""
    from company_envs.world.interact_check import plan_probe

    page = PlanPage(submit="Create board")
    report = plan_probe(page, "http://app.test", "s1", {**A_PLAN, "prepare": []})
    assert "step_error" not in report, "the fake page clicks anything; nothing failed here"
    page.submit = "Create"
    broken = dict(A_PLAN, steps=[{"op": "shout", "role": "button", "name": "Gone"}])
    report = plan_probe(page, "http://app.test", "s1", broken)
    assert "Unknown browser operation" in report["step_error"]
    assert report["marker"] == A_PLAN["marker"], "the report still says what it was looking for"


def test_a_marker_the_state_already_carried_is_not_evidence_of_a_write():
    """The empty-run class: a probe that typed nothing must not file the receipt a write files."""
    from company_envs.world.interact_check import write_reached_state

    report = {"from_plan": True, "marker": "Probe saved value 2026", "collection": "tickets"}
    seeded = {"tickets": [{"subject": "Probe saved value 2026"}]}
    assert write_reached_state(report, seeded, before=seeded) is False
    assert report["marker_present_before"] is True
    assert report["marker_in_collection"] is False


def test_a_recipe_requires_its_own_collection_where_the_generic_probe_cannot():
    """A marker that only reached a toast, a filter or the selection is not a record. A recipe
    names the collection it is aiming at, so that question can be asked of it; the generic probe
    types into whatever it finds and can only ask whether the marker is in the state at all."""
    from company_envs.world.interact_check import write_reached_state

    elsewhere = {"tickets": [], "toast": "Probe saved value 2026"}
    planned = {"from_plan": True, "marker": "Probe saved value 2026", "collection": "tickets"}
    assert write_reached_state(planned, elsewhere, before={"tickets": []}) is True
    assert planned["marker_in_collection"] is False, "the check a plan is graded on is the collection"
    generic = {"marker": "Probe saved value 2026"}
    assert write_reached_state(generic, elsewhere, before={"tickets": []}) is True
    assert "marker_in_collection" not in generic


def test_a_debounced_save_is_waited_for_rather_than_timed():
    """The apps debounce their /post by 300ms and a real company's state is megabytes. Reading once
    after a fixed pause timed the harness instead of the app: gmail, quickbooks and salesforce each
    filed their write on a real world and each read back unchanged."""
    from company_envs.world.interact_check import MARKER_POLLS, plan_probe, wait_for_marker

    page, saved = PlanPage(), []

    def slow_store():
        saved.append(1)
        return {"tickets": [{"subject": "Probe saved value 2026"}]} if len(saved) > 3 else {"tickets": []}

    assert wait_for_marker(page, "Probe saved value 2026", slow_store) is True
    assert len(saved) == 4, "it stops as soon as the write lands, and does not keep polling"
    assert wait_for_marker(page, "never typed", lambda: {"tickets": []}) is False

    def unreadable():
        raise TimeoutError("the app is still rendering")

    assert wait_for_marker(page, "anything", unreadable) is False, "a failed read is not a pass"
    report = plan_probe(page, "http://app.test", "s1", A_PLAN, None, lambda: page.store)
    assert report["marker_seen_before_reload"] is True
    assert MARKER_POLLS >= 10, "shorter than the debounce plus a megabyte write is the old defect"


def test_an_app_that_draws_its_records_is_not_an_empty_screen():
    """canvas_mock renders four shapes and a text object on a <canvas>, so body.innerText is 100
    characters of toolbar and the empty-screen rule failed a working app whose write probe passes.
    Asked of the page, so every drawing app answers it -- not a second list of app names."""
    from company_envs.world.interact_check import shows_something

    class Canvas:
        def __init__(self, drawn):
            self.drawn = drawn

        def evaluate(self, _script):
            return self.drawn

    assert shows_something(Canvas(True), "BoltCanvas Layers line 4 circle 3") is True
    assert shows_something(Canvas(False), "BoltCanvas Layers line 4 circle 3") is False
    assert shows_something(Canvas(False), "x" * 200) is True, "words are still words"

    class Gone:
        def evaluate(self, _script):
            raise TimeoutError("the page went away")

    assert shows_something(Gone(), "") is False, "a page that cannot be asked has shown nothing"
