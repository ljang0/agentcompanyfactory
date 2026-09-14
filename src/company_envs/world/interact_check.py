"""Does a seeded app work when somebody clicks it, and does it hold a real company's volume?

``hub_app.smoke`` proves an app can be seeded and that its first screen contains seeded text.
``app_audit`` drives hand-written probe plans, which exist for a handful of apps. Neither
answers the two questions a task depends on for the rest of the catalogue: can a worker move
around the app without hitting a broken screen, and does what they do reach the state the
grader reads back?

This drives one app with no per-app script. It opens the views a person could reach, clicks
what they would click, then types something and submits it, and reports what changed -- on
screen and in ``/go``'s diff. It also records whether the browser refused a cache write,
which is how a seed too large for the browser's storage announces itself.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .app_audit import BrowserUI, resolve_route, session_url
from .hub_app import HubProcess, build, entry_routes

# Phrases a working screen does not show. NaN stays case-sensitive: "Finance" contains "nan".
# "failed to" with no boundary after it also matches "Failed today", which is what a CI, monitoring
# or log app puts on its own dashboard: circleci's "0 Failed today" tile condemned a working app,
# and the same tile is on datadog, sentry and vercel. An app reporting a failure is doing its job;
# only the page having failed is a finding, so the phrase has to end where the failure ends.
ERROR_TEXT = re.compile(
    r"not found|something went wrong|failed to\b|unable to load|is not defined|undefined is not"
    r"|cannot read propert|error loading|try again later|\[object Object\]|(?-i:\bNaN\b)",
    re.IGNORECASE,
)
SUBMIT_WORDS = re.compile(r"send|save|create|post|add|submit|apply|confirm|comment|reply|new", re.IGNORECASE)
# A control that opens a form to fill in, rather than one that files it.
CREATE_WORDS = re.compile(r"^\s*(?:new\b|create\b|compose\b|add\b|\+$)", re.IGNORECASE)
# Typing into one of these changes nothing, which is how a search box came to stand in for a write.
SEARCH_WORDS = re.compile(r"search|filter|find|query|lookup|jump to", re.IGNORECASE)
# Controls that would end the session or destroy the world instead of exercising it.
SKIP_WORDS = re.compile(r"sign out|log out|logout|delete account|reset|clear all|upgrade", re.IGNORECASE)
CLICK_ROLES = ("tab", "menuitem", "treeitem", "link", "button", "row", "option")
ROLE_ORDER = {role: index for index, role in enumerate(CLICK_ROLES)}
MAX_CLICKS = 18
SETTLE_MS = 450
CLICK_TIMEOUT = 2500


def visible_text(page):
    return page.evaluate("() => (document.body.innerText || '').replace(/\\s+/g, ' ').trim()")


def signature(page):
    """A fingerprint of the screen, to tell a click that did something from one that did not."""
    return page.evaluate(
        "() => { const t = (document.body.innerText || '').replace(/\\s+/g, ' ').trim();"
        " return [document.querySelectorAll('*').length, t.length, t.slice(0, 400)].join('|'); }"
    )


# A screen that draws its records instead of writing them: canvas_mock renders four shapes and a
# text object on a <canvas>, and ``document.body.innerText`` is 100 characters of chrome, so the
# empty-screen rule condemned a working app whose own write probe passes. Asked of the page rather
# than of a list of app names, so every drawing app answers it.
DRAWN_CONTENT = """() => [...document.querySelectorAll('canvas,svg,img')].some(el => {
  const box = el.getBoundingClientRect();
  return box.width >= 200 && box.height >= 200;
})"""


def shows_something(page, text):
    """Whether the first screen has anything on it: words, or something large enough to be a view."""
    if len(text) > 120:
        return True
    try:
        return bool(page.evaluate(DRAWN_CONTENT))
    except Exception:  # noqa: BLE001 -- a page that cannot be asked is not showing anything
        return False


def storage_overflow(page):
    """How many cache writes the browser refused: a seed too large for this origin's quota."""
    return page.evaluate("() => window.__hubStorageOverflow || 0")


# A broken screen says so where a person would see it, and has little else on it. Deep inside a
# long page the same words are usually content, so the phrase is looked for in the page's headline,
# or anywhere at all once the screen is too sparse to be showing records.
HEADLINE = 1200
SPARSE_SCREEN = 600
# An app whose records *are* error strings cannot be judged by this rule at all, and no positional
# window saves it: sentry_mock seeds 2,048 issue titles like "TypeError: Cannot read properties of
# undefined", and its very first row is one. It passed every other check, in a live guest included,
# so the rule is switched off for it by name rather than loosened for everybody.
ERROR_DOMAIN_APPS = frozenset({"sentry_mock"})


def error_near(text, limit=HEADLINE):
    """The failure phrase a broken screen is showing, with enough around it to read, or None.

    Looked for in the headline of the page, and anywhere at all only when the screen is too sparse
    to be showing records -- which is what a failure phrase beside no content means.
    """
    window = text if len(text) < SPARSE_SCREEN else text[:limit]
    found = ERROR_TEXT.search(window)
    return window[max(0, found.start() - 60) : found.end() + 60] if found else None


def screen_error(app_id, text):
    """``error_near``, unless this app's own records read like failures."""
    return None if app_id in ERROR_DOMAIN_APPS else error_near(text)


# How a browser names a control, in the order the accessible-name calculation uses: the elements
# a label points at, then an explicit label, then the native label for a form control, then what
# the control says, then its tooltip, then what it is asking for. Reading fewer of these is the
# narrow-vocabulary defect: circleci names all 16 of its buttons with a ``title`` and nothing else,
# so a reader that stopped at aria-label and text found **zero** clickable targets on a working app
# and filed it as dead. microsoft_teams has 32 such buttons, miro 20, slack 12, jira 10. This is
# the name ``get_by_role(role, name=...)`` matches on, so what is listed here can be found again.
TARGET_FACTS = """el => {
  const words = value => (value || '').replace(/\\s+/g, ' ').trim();
  const from = ids => words((ids || '').split(/\\s+/)
    .map(id => { const node = document.getElementById(id); return node ? node.innerText : ''; })
    .join(' '));
  const labels = el.labels ? [...el.labels].map(node => node.innerText).join(' ') : '';
  const name = from(el.getAttribute('aria-labelledby')) || words(el.getAttribute('aria-label'))
    || words(labels) || words(el.tagName === 'INPUT' ? el.value : '') || words(el.alt)
    || words(el.innerText || el.textContent) || words(el.getAttribute('title'))
    || words(el.getAttribute('placeholder'));
  // Off-canvas, not merely below the fold: a closed drawer parks its controls at translateX(-100%)
  // and every one of them is visible to checkVisibility and unclickable to a person. Below the
  // fold stays, because the click pass scrolls to those the way a person does.
  const box = el.getBoundingClientRect();
  const reachable = box.width > 0 && box.height > 0 && box.right > 0 && box.bottom > 0
    && box.left < (window.innerWidth || 0);
  return {name, reachable};
}"""


def targets(page, limit=MAX_CLICKS):
    """What a person could click, named as a screen reader would read it.

    Collected as (role, name) rather than handles: a click rebuilds the DOM and every held
    handle goes stale, so each target is located again when its turn comes.
    """
    found, seen = [], set()
    for role in CLICK_ROLES:
        try:
            items = page.get_by_role(role).all()
        except Exception:  # noqa: BLE001, S112 -- a role this page does not use is not a finding
            continue
        for item in items[:60]:
            try:
                if not item.is_visible() or not item.is_enabled():
                    continue
                facts = item.evaluate(TARGET_FACTS)
                if not facts["reachable"]:
                    continue
                name = facts["name"].strip()
            except Exception:  # noqa: BLE001, S112 -- an element that vanished while listing is not a finding
                continue
            name = re.sub(r"\s+", " ", name)[:60]
            if not name or SKIP_WORDS.search(name) or (role, name.lower()) in seen:
                continue
            seen.add((role, name.lower()))
            found.append({"role": role, "name": name})
    # Navigation first: it opens the most views per click, which is what breadth means here.
    found.sort(key=lambda target: ROLE_ORDER.get(target["role"], len(CLICK_ROLES)))
    return found[:limit]


# The bookkeeping Playwright narrates while it retries a click. What is left when these are
# taken out is the sentence that says why the click never landed.
CLICK_LOG_NOISE = re.compile(
    r"^(?:attempting|retrying|performing) click action$|^waiting \d+ms$|^waiting for element to be"
    r"|^element is visible|^scrolling into view|^done scrolling|^locator resolved"
    r"|^waiting for locator|^waiting for get_by_role|^Call log",
    re.IGNORECASE,
)


def click_failure(exc):
    """Why a click could not land, from Playwright's call log -- not just its first line.

    "Timeout 2500ms exceeded" alone cannot tell an element that is not there from one an overlay
    is covering, and that is the difference between an app defect and a screen the probe should
    never have been clicking on. 605 of the 1,214 clicks in the recorded run said only that.
    """
    lines = [line.strip().lstrip("- ").strip() for line in str(exc).splitlines()]
    lines = [line for line in lines if line]
    head = f"{type(exc).__name__}: {lines[0][:100]}" if lines else type(exc).__name__
    spoken = [line for line in lines[1:] if not CLICK_LOG_NOISE.match(line)]
    return f"{head} | {spoken[-1][:140]}" if spoken else head


def same_app(url):
    """The origin a click has to stay inside to still be this app.

    Compared against the origin rather than against the whole landing URL: 19 apps land a worker
    on a path of their own, and every in-app route away from that path would otherwise read as
    the click having left the site.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def click_pass(page, url, items, faults, app_id=""):
    """Click each target once, each on the screen it was listed on; record what changed and broke.

    Returning to that screen is half the job. A click that opens a modal, switches an in-app route
    or drops the ``?sid=`` leaves behind a screen the remaining targets are not on, and every one
    of them then fails to click -- which the report filed as the app's own buttons not working.
    Only a click that left the origin was ever recovered from.
    """
    results = []
    listing = signature(page)
    for item in items:
        seen_faults = len(faults)
        row = {**item, "clicked": False, "changed": False, "error": None, "error_text": None}
        before = signature(page)
        if before != listing or page.url != url:
            # A reload zeroes the browser's count of refused cache writes, so read it first.
            row["storage_overflow"] = storage_overflow(page)
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(SETTLE_MS)
            # Whatever a reload gives back is the screen to judge from here on: an app that keeps
            # its route across a reload would otherwise be reloaded before every remaining target.
            before = listing = signature(page)
            row["restored"] = True
        target = page.get_by_role(item["role"], name=item["name"], exact=False).first
        row["clicked"], row["error"], recovery = click_with_recovery(page, target)
        if recovery:
            row["recovered"] = recovery
        if not row["clicked"]:
            results.append(row)
            continue
        page.wait_for_timeout(SETTLE_MS)
        if not page.url.startswith(same_app(url)):
            # A click left the app. Come back before reading the screen: another site's 404 is
            # not this app's error text.
            row["left_app"] = page.url[:120]
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(SETTLE_MS)
        elif page.url != url:
            # An in-app route change, including one that dropped the ?sid=. The screen it opened
            # is the one to judge this click by; the next target is restored before it is clicked.
            row["routed_to"] = page.url[:120]
        text = visible_text(page)
        row["changed"] = signature(page) != before
        row["error_text"] = screen_error(app_id, text)
        row["empty_screen"] = len(text) < 120
        row["faults"] = faults[seen_faults:][:3]
        results.append(row)
    return results


# What Playwright says when a click could not land for a reason a person would work around.
OFF_SCREEN = re.compile(r"outside of the viewport", re.IGNORECASE)
COVERED = re.compile(r"intercepts pointer events|subtree intercepts", re.IGNORECASE)
# Clicking through the element on top only where it is a separate thing: a backdrop, a drawer, a
# menu left open. Never where it contains the target, which would be clicking the target twice.
DISMISS_INTERCEPTOR = """([element, box]) => {
  const drawn = el => el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  // A drawer's own backdrop is put there to be clicked: epic-health renders
  // <div class="sidebar-overlay" onClick={handleClose}>. Dismiss that first, because the element
  // actually on top of the target is usually a control inside the drawer, which closes nothing.
  const backdrop = [...document.querySelectorAll(
    '[class*=overlay],[class*=backdrop],[class*=scrim],[data-overlay]')].find(drawn);
  if (backdrop && !backdrop.contains(element)) { backdrop.click(); return 'backdrop'; }
  const top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
  if (!top || top === element || top.contains(element) || element.contains(top)) return null;
  top.click();
  return (top.getAttribute('class') || top.tagName).slice(0, 60);
}"""


def click_with_recovery(page, target, timeout=CLICK_TIMEOUT):
    """Click, and work around the two obstacles a person works around without noticing.

    A control below the fold is scrolled to, and a control under an open drawer is reached by
    dismissing the drawer the way its own backdrop is meant to be dismissed -- epic-health's
    ``.sidebar-overlay`` carries ``onClick={handleClose}`` and swallowed **13 of 18** clicks,
    every one of them filed as the app's buttons not working. Nothing here retries a click that
    failed for any other reason, and the interceptor is only clicked when it is a separate
    element from the target.

    Returns ``(clicked, error, recovery)``.
    """
    for attempt in range(3):
        try:
            target.click(timeout=timeout)
            return True, None, ATTEMPT_NAMES[attempt]
        except Exception as exc:  # noqa: BLE001 -- a click that cannot land is the result
            failure = click_failure(exc)
        try:
            if OFF_SCREEN.search(failure):
                target.scroll_into_view_if_needed(timeout=timeout)
                continue
            if COVERED.search(failure):
                page.keyboard.press("Escape")
                page.wait_for_timeout(SETTLE_MS)
                box = target.bounding_box(timeout=timeout)
                element = target.element_handle(timeout=timeout)
                if box and element and page.evaluate(DISMISS_INTERCEPTOR, [element, box]):
                    page.wait_for_timeout(SETTLE_MS)
                    continue
        except Exception as workaround_error:  # noqa: BLE001 -- the original failure is the result
            failure = f"{failure} | workaround: {click_failure(workaround_error)[:80]}"
        return False, failure, None
    return False, failure, None


ATTEMPT_NAMES = (None, "reached on the second try", "reached on the third try")


def text_boxes(page):
    """Visible places a person could type, search fields last.

    Typing into a search box changes nothing, and it was the first box on the page in several apps
    -- gmail's "Search mail" stood in for a write and the diff that followed came from elsewhere.
    """
    found = []
    for selector in ("textarea", "input[type=text]", "input:not([type])", "[contenteditable=true]"):
        try:
            found += [box for box in page.locator(selector).all() if box.is_visible() and box.is_enabled()]
        except Exception:  # noqa: BLE001, S112 -- a selector this page does not use is not a finding
            continue

    def is_search(box):
        try:
            label = (box.get_attribute("placeholder") or box.get_attribute("aria-label") or "") + (
                box.get_attribute("name") or ""
            )
        except Exception:  # noqa: BLE001 -- an element that went away is not a search box
            return True
        return bool(SEARCH_WORDS.search(label))

    return sorted(found, key=is_search)


def open_a_form(page):
    """Click what a person clicks to start something new, so there is a form to fill in.

    Without this the probe types into whatever box the page already shows, which on most apps is a
    filter. The label of the control that worked is returned, or None.
    """
    for role in ("button", "link", "menuitem"):
        try:
            controls = page.get_by_role(role).all()[:40]
        except Exception:  # noqa: BLE001, S112 -- a role this page does not use is not a finding
            continue
        for control in controls:
            try:
                label = (control.get_attribute("aria-label") or control.inner_text() or "").strip()
                if not CREATE_WORDS.match(label) or SKIP_WORDS.search(label):
                    continue
                if not control.is_visible() or not control.is_enabled():
                    continue
                before = len(text_boxes(page))
                control.click(timeout=2500)
                page.wait_for_timeout(700)
                if len(text_boxes(page)) > before:
                    return label[:60]
            except Exception:  # noqa: BLE001, S112 -- try the next plausible control
                continue
    return None


def mutation_probe(page):
    """Start something new, fill it in and file it; the marker is what to look for in state."""
    marker = f"interact probe {int(time.time())}"
    report = {"attempted": False, "typed_into": None, "submitted": None, "marker": marker}
    report["opened_form"] = open_a_form(page)
    boxes = text_boxes(page)
    for box in boxes[:4]:
        try:
            label = box.get_attribute("placeholder") or box.get_attribute("aria-label") or "(unnamed)"
            box.click(timeout=2000)
            if box.get_attribute("contenteditable") is None:
                box.fill(marker)
            else:
                box.type(marker)
            report.update(attempted=True, typed_into=label[:60])
        except Exception as exc:  # noqa: BLE001 -- a box that will not take text is the result
            report["type_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            continue
        for button in page.get_by_role("button").all()[:40]:
            try:
                label = (button.get_attribute("aria-label") or button.inner_text() or "").strip()
                if not SUBMIT_WORDS.search(label) or not button.is_visible() or not button.is_enabled():
                    continue
                button.click(timeout=2500)
                report["submitted"] = label[:60]
                page.wait_for_timeout(900)
                return report
            except Exception:  # noqa: BLE001, S112 -- try the next plausible submit control
                continue
        try:
            box.press("Enter")
            report["submitted"] = "Enter"
            page.wait_for_timeout(900)
            return report
        except Exception:  # noqa: BLE001, S112 -- nothing submitted from this box; try the next
            continue
    return report


class _Steps:
    """Run a probe plan's steps on a page this module already has open.

    The step vocabulary -- fill, click, select_option, press, addressed by role, placeholder,
    visible text, CSS selector and position -- is ``app_audit.BrowserUI``'s, borrowed rather than
    restated so the two probes cannot drift into disagreeing about what a recipe means.
    """

    step, pause = BrowserUI.step, BrowserUI.pause

    def __init__(self, page):
        self.page = page


# How long to wait for a write to reach the store, in ``SETTLE_MS`` steps. The apps debounce their
# ``/post`` by 300ms and a real company's state is megabytes, so reading once after a fixed pause
# times the harness rather than the app: gmail, quickbooks and salesforce all filed their write and
# all three read back unchanged, and all three land inside two seconds when the read waits for them.
MARKER_POLLS = 20


def wait_for_marker(page, marker, state_of):
    """Give the app the time its debounced save takes, and no more."""
    for _ in range(MARKER_POLLS):
        try:
            if marker in json.dumps(state_of(), ensure_ascii=False):
                return True
        except Exception as unread:  # noqa: BLE001 -- a state read that failed is not a verdict
            del unread  # try again; the caller's own read is the one that decides
        page.wait_for_timeout(SETTLE_MS)
    return False


def plan_probe(page, base_url, sid, plan, state=None, state_of=None):
    """Replay this app's hand-authored write recipe, and say what stopped it if it stopped.

    The generic ``mutation_probe`` is a search, not a specification: it types into whatever box it
    can find. On 88 apps it reached grader-visible state on 20 while hand-authored plans reached it
    on 75, so a report that consults only the generic probe understates the catalogue by 55 apps.
    Where a plan exists it is the measurement and the generic probe is the fallback.

    The bar is ``app_audit.write_probe``'s and does not move: the typed marker has to reach the
    named collection in ``/state`` and still be there after a reload.
    """
    report = {
        "attempted": False,
        "from_plan": True,
        "marker": plan["marker"],
        "collection": plan["collection"],
        "route": plan.get("route", "/"),
        "typed_into": None,
        "submitted": None,
    }
    route = resolve_route(plan.get("route", "/"), state or {})
    try:
        page.goto(session_url(base_url, route, sid), wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(SETTLE_MS)
        runner = _Steps(page)
        for step in plan.get("prepare", []):
            runner.step(step)
        for step in plan["steps"]:
            runner.step(step)
            if step["op"] in {"fill", "select_option"}:
                report["attempted"] = True
                report["typed_into"] = step.get("placeholder") or step.get("name") or step.get("selector")
            else:
                report["submitted"] = step.get("name") or step.get("text") or step.get("selector")
    except Exception as exc:  # noqa: BLE001 -- a recipe that no longer fits its app is the result
        report["step_error"] = click_failure(exc)
    page.wait_for_timeout(900)
    if state_of is not None:
        report["marker_seen_before_reload"] = wait_for_marker(page, plan["marker"], state_of)
    # Read the state back only after a reload, so a value that lives in the page and never
    # reached the app's store cannot pass.
    try:
        page.goto(session_url(base_url, route, sid), wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(SETTLE_MS)
    except Exception as exc:  # noqa: BLE001 -- keep the write result; say the reload failed
        report["reload_error"] = click_failure(exc)
    return report


def write_reached_state(report, state, before=None):
    """Whether the probe's marker is in the collection it aimed at, or anywhere at all.

    A plan names the collection its marker has to land in; the generic probe cannot, and for it
    anywhere in the state is the only question that can be asked.

    A marker the state already carried before the probe typed anything answers for a write that
    never happened, which is the receipt-of-an-empty-run defect: it is recorded and refused.
    """
    marker = report["marker"]
    if before is not None and marker in json.dumps(before, ensure_ascii=False):
        report["marker_present_before"] = True
        report["marker_in_collection"] = False
        return False
    if report.get("from_plan"):
        report["marker_in_collection"] = marker in json.dumps(
            state.get(report["collection"]), ensure_ascii=False
        )
    return marker in json.dumps(state, ensure_ascii=False)


def readable(value):
    """Whether a leaf string is something a screen would show a person.

    A space is not the test. Requiring one skipped every value in a Chinese-language app, which
    reported dingtalk and greenhouse as not rendering their seeded records when a screenshot shows
    that they plainly do.
    """
    if not isinstance(value, str) or not 4 <= len(value) <= 60 or value.startswith(("http", "data:")):
        return False
    if " " in value or any(ord(character) > 0x2E80 for character in value):
        return True
    # A bare token is an identifier unless it reads as a word: letters, and not a hex or id string.
    return value.isalpha() and len(value) >= 6


def sample_strings(state, limit=40):
    """Readable leaf values from across a seeded state, not from whichever branch comes first.

    Taken breadth-first, a few per top-level collection in turn. Depth-first filled the whole
    sample from one buried collection, so a screen showing nine other collections' records read as
    showing none of them.
    """
    per_collection = {}

    def walk(bucket, node, depth=0):
        if len(per_collection.setdefault(bucket, [])) >= 8 or depth > 6:
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(bucket, value, depth + 1)
        elif isinstance(node, list):
            for value in node[:8]:
                walk(bucket, value, depth + 1)
        elif readable(node):
            per_collection[bucket].append(node)

    for key, value in state.items():
        walk(key, value)
    found, round_number = [], 0
    while len(found) < limit and any(len(v) > round_number for v in per_collection.values()):
        for values in per_collection.values():
            if round_number < len(values) and len(found) < limit:
                found.append(values[round_number])
        round_number += 1
    return found


def _with_suffix(record, suffix):
    """A copy of one record under a new identity, so a collection can hold more of them."""
    if not isinstance(record, dict):
        return record
    copy = dict(record)
    for key, value in record.items():
        if isinstance(value, str) and (key == "id" or key.endswith(("Id", "_id", "ID"))):
            copy[key] = f"{value}{suffix}"
    return copy


# How deep a collection can sit before amplification stops looking for it. Nesting is the point:
# canvas_mock's only collection is ``canvasJSON.objects``, so a top-level search grew nothing and
# left a 1,764-byte state to stand in for a 3 MB one.
COLLECTION_DEPTH = 6


def collections_to_grow(state):
    """Every collection of records in a state, however deeply nested.

    A collection is a non-empty list, or a dict whose every value is a dict -- a map of records
    keyed by id. The walk stops at one: the lists inside a record are that record's own fields,
    not collections to multiply. The state itself is always walked into, never doubled as a map.
    """

    def walk(node, depth):
        if depth > COLLECTION_DEPTH:
            return []
        if isinstance(node, list):
            return [node] if node else []
        if isinstance(node, dict):
            if node and all(isinstance(value, dict) for value in node.values()):
                return [node]
            return [found for value in node.values() for found in walk(value, depth + 1)]
        return []

    if not isinstance(state, dict):
        return walk(state, 1)
    return [found for value in state.values() for found in walk(value, 1)]


def amplify(state, target_bytes):
    """Grow a fixture state to the size a real company's records reach, and say whether it got there.

    Each round doubles what every collection already holds, under fresh identities, so a few
    rounds span three orders of magnitude. This asks whether an app survives a company's
    volume -- storage quota, render time, list virtualisation -- not whether the records are
    coherent with one another; a real world state is what proves that.

    Returns ``(grown, report)``. The report is the half that matters to a caller: a state that
    could not be grown has not been volume tested, whatever else the run went on to see.
    """
    grown = json.loads(json.dumps(state))
    start = len(json.dumps(grown))
    rounds, widest = 0, 0
    for round_number in range(1, 40):
        if len(json.dumps(grown)) >= target_bytes:
            break
        found = collections_to_grow(grown)
        if not found:
            break
        suffix = f"-v{round_number}"
        for node in found:
            if isinstance(node, list):
                node.extend(_with_suffix(record, suffix) for record in list(node))
            else:
                node.update(
                    {f"{name}{suffix}": _with_suffix(record, suffix) for name, record in list(node.items())}
                )
        rounds, widest = round_number, max(widest, len(found))
    size = len(json.dumps(grown))
    return grown, {
        "start_bytes": start,
        "bytes": size,
        "target_bytes": target_bytes,
        "rounds": rounds,
        "collections": widest,
        "grew": size > start,
        "reached_target": size >= target_bytes,
    }


def amplify_state(state, target_bytes):
    """``amplify`` for a caller that wants the grown state and not the account of growing it."""
    return amplify(state, target_bytes)[0]


def interact_app(
    app_id,
    source,
    work,
    state,
    browser,
    *,
    sid="interact",
    max_clicks=MAX_CLICKS,
    shot=None,
    log_dir=None,
    plan=None,
):
    """Build, serve, seed and drive one app. Returns the report; never raises for the app's sake."""
    work = Path(work)
    log_dir = Path(log_dir) if log_dir else work
    log_dir.mkdir(parents=True, exist_ok=True)
    report = {"app_id": app_id, "seeded": state is not None, "checks": {}}
    if state is not None:
        report["state_bytes"] = len(json.dumps(state))
    built = build(source, work / app_id, log=log_dir / f"{app_id}-build.log")
    faults = []
    with HubProcess(built, log=log_dir / f"{app_id}-serve.log") as server:
        client = server.client
        if state is not None:
            client.seed(sid, state)
            seen = client.inspect(sid)
            report["checks"]["seed_readback"] = seen["initial_state"] == state
            report["checks"]["no_diff_after_seed"] = not seen["state_diff"]
        page = browser.new_context(viewport={"width": 1600, "height": 1000}).new_page()
        # Pictures the browser could not fetch. This harness serves the app directly, so it sees
        # the seeded URL rather than the placeholder the worker proxy substitutes for it -- which
        # makes this evidence about the seed, not about what a worker sees, and is why it is
        # recorded rather than failed on. It is also the only place a regression in that
        # substitution would show up as a number, so the number is kept.
        unfetchable = []
        page.on(
            "requestfailed",
            lambda request: unfetchable.append(f"{request.url[:120]} {request.failure or ''}"[:200])
            if request.resource_type == "image"
            else None,
        )
        page.on("pageerror", lambda exc: faults.append(str(exc).splitlines()[0][:180]))
        page.on(
            "console",
            lambda message: (
                faults.append(f"console: {message.text[:160]}") if message.type == "error" else None
            ),
        )
        # The page a worker is landed on, which for 19 apps is not "/": Zendesk's landing screen
        # shows 6 of 200 of the company's own values and /customers shows 197, and a probe that
        # opens "/" has been clicking a launcher rather than the app.
        entry = entry_routes().get(app_id, "/")
        url = session_url(server.client.base_url, entry, sid)
        report["entry_path"] = entry
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        text = visible_text(page)
        report["first_screen"] = {"text_length": len(text), "error_text": screen_error(app_id, text)}
        report["checks"]["first_screen_not_empty"] = shows_something(page, text)
        report["checks"]["first_screen_no_error"] = report["first_screen"]["error_text"] is None
        report["storage_overflow"] = storage_overflow(page)
        if state is not None:
            shown = [value for value in sample_strings(state) if value in text]
            report["seeded_strings_shown"] = shown[:5]
            report["checks"]["shows_seeded_content"] = bool(shown)
        found = targets(page, max_clicks)
        report["clickable_found"] = len(found)
        report["clicks"] = click_pass(page, url, found, faults, app_id)
        worked = [click for click in report["clicks"] if click["clicked"] and click["changed"]]
        report["click_summary"] = {
            "tried": len(report["clicks"]),
            "changed_screen": len(worked),
            "dead": len([c for c in report["clicks"] if c["clicked"] and not c["changed"]]),
            "failed_to_click": len([c for c in report["clicks"] if c["error"]]),
            "showed_error_text": len([c for c in report["clicks"] if c["error_text"]]),
            # How often the screen the targets were listed on had to be reloaded under them.
            "restored": len([c for c in report["clicks"] if c.get("restored")]),
        }
        # A third of the reachable controls doing something is a working app; demanding more
        # would fail every app whose nav has a currently-selected item.
        report["checks"]["clicks_mostly_work"] = bool(found) and len(worked) >= max(1, len(found) // 3)
        report["checks"]["no_click_shows_an_error"] = not any(c["error_text"] for c in report["clicks"])
        # The probe runs from wherever the click pass ended, which is where a worker who had been
        # clicking around would be. Reloading the landing screen first was measured over all 88
        # apps and is a wash -- it wins a write on three apps and loses one on two others, inside
        # the run-to-run noise of this probe -- so the page is left where the clicking left it.
        seen_before = client.inspect(sid)["current_state"] if plan else None

        def current():
            return client.inspect(sid)["current_state"]

        if plan:
            report["mutation"] = plan_probe(page, client.base_url, sid, plan, state, current)
        else:
            report["mutation"] = mutation_probe(page)
            wait_for_marker(page, report["mutation"]["marker"], current)
        final = client.inspect(sid)
        report["mutation"]["marker_in_state"] = write_reached_state(
            report["mutation"], final["current_state"], seen_before
        )
        if plan:
            report["checks"]["plan_write_reaches_state"] = report["mutation"]["marker_in_collection"]
        # The one check a task depends on: the grader reads this diff, not the screen.
        report["checks"]["ui_action_reaches_state"] = bool(final["state_diff"])
        # The most refusals seen in any one page load: the count restarts at every reload.
        report["storage_overflow_after"] = max(
            [storage_overflow(page), report["storage_overflow"]]
            + [click.get("storage_overflow") or 0 for click in report["clicks"]]
        )
        if shot:
            Path(shot).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot))
            report["screenshot"] = str(shot)
        report["faults"] = faults[:12]
        report["checks"]["no_browser_faults"] = not faults
        report["unfetchable_images"] = {"count": len(unfetchable), "first": unfetchable[:5]}
        page.context.close()
    failed = sorted(name for name, ok in report["checks"].items() if not ok)
    report["failed_checks"] = failed
    report["passed"] = not failed
    return report


__all__ = [
    "CLICK_LOG_NOISE",
    "CLICK_TIMEOUT",
    "COLLECTION_DEPTH",
    "ERROR_DOMAIN_APPS",
    "ERROR_TEXT",
    "HEADLINE",
    "MAX_CLICKS",
    "SKIP_WORDS",
    "TARGET_FACTS",
    "amplify",
    "amplify_state",
    "click_failure",
    "click_pass",
    "click_with_recovery",
    "collections_to_grow",
    "error_near",
    "interact_app",
    "mutation_probe",
    "open_a_form",
    "plan_probe",
    "readable",
    "same_app",
    "sample_strings",
    "screen_error",
    "shows_something",
    "storage_overflow",
    "targets",
    "text_boxes",
    "wait_for_marker",
    "write_reached_state",
]
