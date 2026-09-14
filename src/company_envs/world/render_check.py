"""Browser-level check that every served hub app renders its seeded state.

``hub-serve`` proves the state round trip over HTTP; an app can still sit on its loading screen
in a browser (a localStorage quota error inside the load promise, a shape the loader cannot
read) and nothing before the VM stage would notice. This visits each app's first worker proxy
URL in headless Chrome with a fresh profile, as a new VM browser would, judges the visible text,
and confirms the read-only visit wrote nothing back. It reads ``runtime/endpoints.json`` from a
running ``hub-serve`` and writes ``runtime/RENDER.json``; it starts no servers and calls no model.

Two things it used to measure and then throw away, across 20 rendered companies and 132 passing
app renders:

* the seeded-strings count was reported and never failed an app, so 71 renders passed showing
  under a tenth of the company's own values -- ``google_drive_mock`` passed in all 20 companies
  on 1 of 400 strings and about 210 visible characters. :func:`content_floor` now decides.
* every check opened ``/``, which for several apps is a launcher rather than the workspace. The
  route an app lands a worker on is recorded in :data:`ENTRY_ROUTES` and honoured here.

Hydration time is not one of them. Measured on this host, ``--virtual-time-budget`` fast-forwards
idle time, so the same page dumps a byte-identical DOM at a 3-second and a 30-second budget; a
guest's ~3x slowdown is wall clock, and this gate's browser never runs in a guest. The numbers a
guest does need are published in the report (see :data:`GUEST_SLOWDOWN`) for the VM launcher.
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import urlopen

from company_envs.receipt import OUTCOME_OK, PASSED, Receipt, faulted, outcome_of, rollup
from company_envs.storage import decided_by, now, read, write

BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
BROWSER_VARIABLE = "COMPANY_ENVS_BROWSER"
LOADING_TEXT_CAP = 120  # visible text this short that says "Loading" is a loading screen, not an app
STRING_LENGTH = (12, 60)  # seeded string values this long, with a space, are what a viewer would read
STRING_CAP = 400
SHOT_DIR = "runtime/render"  # one JPEG per app, the first view a worker would see
SETTLE_SECONDS = 3  # a hydrated tab that writes back does so within moments of its first render
VIRTUAL_TIME_MS = 30_000
# A guest VM hydrates about three times slower than this host: bamboohr shows 3,172 characters
# and no seeded strings four seconds after load, and 198,993 -- the host's own figure -- after
# twelve. Nothing here can be measured in a guest, so the report publishes the number the VM
# launcher and the guest screenshot step have to wait out; it does not change what is rendered.
GUEST_SLOWDOWN = 3
BROWSER_TIMEOUT = 120
NO_BROWSER = f"no headless browser found ({', '.join(BROWSERS)}; set {BROWSER_VARIABLE})"


def find_browser(env=None):
    """The browser binary: ``COMPANY_ENVS_BROWSER`` when set, else the first known name on PATH."""
    env = os.environ if env is None else env
    chosen = env.get(BROWSER_VARIABLE)
    if chosen:
        return chosen if Path(chosen).is_file() else shutil.which(chosen)
    return next((path for name in BROWSERS if (path := shutil.which(name))), None)


def visible_text(dom):
    """What a viewer reads: script, style and comment blocks gone, tags stripped, whitespace folded."""
    dom = re.sub(
        r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", " ", dom, flags=re.DOTALL | re.IGNORECASE
    )
    dom = re.sub(r"<!--.*?-->", " ", dom, flags=re.DOTALL)
    dom = re.sub(r"<[^>]+>", " ", dom)
    return " ".join(html.unescape(dom).split())


def is_loading(text):
    """A short page that says Loading is the loading screen (the title may precede the word)."""
    return len(text) < LOADING_TEXT_CAP and re.search(r"\bloading\b", text, re.IGNORECASE) is not None


def seeded_strings(state, cap=STRING_CAP):
    """Distinct human-sized string values of a seeded state, in document order, at most CAP."""
    low, high = STRING_LENGTH
    found = {}

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and low <= len(value) <= high and " " in value.strip():
            found.setdefault(" ".join(value.split()), None)

    walk(state)
    return list(found)[:cap]


def strings_seen(strings, text):
    return sum(1 for s in strings if s in text)


def stored(envelope):
    """The ``stored_state`` of a ``/state`` envelope; a bare state is taken as it is."""
    if isinstance(envelope, dict) and "stored_state" in envelope:
        return envelope["stored_state"]
    return envelope


# A first view that says one of these is showing a failure, not a workspace. Matched against the
# opening of the visible text so an email that happens to contain "error" is not a finding.
BROKEN_TEXT = re.compile(
    r"\b(?:not found|404 not found|http 404|error 404|500 internal server error"
    r"|internal server error|application error|unhandled runtime error|something went wrong"
    r"|failed to (?:load|fetch)|cannot read propert)",
    re.IGNORECASE,
)
# Content the app shipped as its own template, or a value that leaked from the code into the page.
PLACEHOLDER_TEXT = re.compile(
    r"\[object Object\]"
    r"|\b(?:lorem ipsum|coming soon|not implemented|placeholder|undefined|NaN"
    r"|John Doe|Jane Doe|Alex Johnson)\b"
)
OPENING = 400  # characters of the first view judged for failure text
CONTROLS = re.compile(r"<(?:button|a\s|input|textarea|select)\b|role=[\"']button[\"']", re.IGNORECASE)
IMAGES = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
IMAGE_SOURCE = re.compile(r"\bsrc\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
APP_DOM = 2000  # a rendered app's DOM is tens of kilobytes; below this it is a stub, not a screen
# Images the browser cannot draw fail an app. The proxy rewrites every image reference a VM
# browser could not fetch to a local placeholder, so one reaching a rendered page means that
# rewriting missed it: a regression, not a world the generator could have avoided.
BLOCK_BROKEN_IMAGES = True


def controls(dom):
    """Clickable affordances in the rendered DOM; a page with none is a picture, not an app."""
    return len(CONTROLS.findall(dom or ""))


def broken_images(dom):
    """Image sources a browser cannot draw: empty, or pointing outside the page's own world.

    The VM has no route off the host, so an image on a public CDN renders as a broken box; an
    empty ``src`` always does. Both are the defects that made avatars and logos boxes with
    crosses, which the DOM shows and the visible text cannot.
    """
    found = []
    for tag in IMAGES.findall(dom or ""):
        match = IMAGE_SOURCE.search(tag)
        source = (match.group(1) if match else "").strip()
        if re.match(r"https?://", source, re.IGNORECASE):
            found.append(source[:120])
    return found


def empty_images(dom):
    """Images an app draws from its own empty default: reported, never blocking.

    An ``<img src="">`` built at runtime from a value the app supplies itself is a defect in the
    clone, not in the world, and no generated company can repair it. A remote URL is different:
    the proxy replaces every one, so seeing one is a regression worth failing.
    """
    return sum(
        1
        for tag in IMAGES.findall(dom or "")
        if not (m := IMAGE_SOURCE.search(tag)) or not m.group(1).strip()
    )


FLOOR_STRINGS = 5  # distinct seeded values a first view must show before it counts as the world
# ...unless the page carries this much visible text. A seeded string is a value 12-60 characters
# long matched exactly, so a product that truncates a cell, splits a message across elements or
# renders a total instead of a row shows the world and matches nothing: california-civil-rights'
# Slack renders 16,852 characters of real conversation and matches 4 of its 22 sampled strings.
# Above this much text the shortfall is the matcher, not the page; below it, it is the page.
FLOOR_TEXT = 1500


def content_floor(seen, total, chars, dom=None):
    """Why this first view does not show the company's world, or None when it does.

    ``verdict`` used to say outright that "the seeded-strings count is reported by the caller and
    never fails an app". Measured over the 20 rendered companies: 71 of the 132 passing app
    renders showed under a tenth of their seeded strings, ``google_drive_mock`` passing in all 20
    companies and in 19 of them on 2 or fewer of up to 400 strings in ~210 visible characters,
    and three visual judges then voted one of those drives ``looks_real`` and ``works``.

    No app is exempt from this by name. The two ways a good screen clears it were both measured:
    a workspace route (``google_sheets_mock`` shows 1 of 400 strings on ``/`` and 54 on
    ``/spreadsheet``), and text volume (Slack above; and the canvas-drawn apps need neither --
    ``tableau_mock`` shows 8 of 105, ``miro_mock`` 13 of 140, ``PACS-viewer_mock`` 7 of 18, all
    from the record names in their own chrome).
    """
    if total <= 0 or len(dom or "") <= APP_DOM:
        # A state with no human-sized values has nothing to be missing from the page, and a page
        # whose whole DOM is smaller than one app screen is a stub, not a workspace -- the same
        # line the affordance check draws, so a test double is judged by neither.
        return None
    wanted = min(FLOOR_STRINGS, total)
    if seen >= wanted or chars >= FLOOR_TEXT:
        return None
    return (
        f"the first view shows {seen} of {total} of this company's own values in {chars} visible "
        f"characters: a workspace must show at least {wanted} of them, or {FLOOR_TEXT} characters "
        "of its own text"
    )


def failure_text(text):
    """The failure phrase a first view opens with, if any."""
    match = BROKEN_TEXT.search((text or "")[:OPENING])
    return match.group(0) if match else None


def placeholder_text(text):
    """Template or leaked-value text anywhere in the first view."""
    seen = []
    for match in PLACEHOLDER_TEXT.finditer(text or ""):
        if match.group(0) not in seen:
            seen.append(match.group(0))
    return seen[:6]


def verdict(text, before, after, *, error=None, dom=None, seen=0, total=0):
    """One app's verdict from its visible text, the two ``/state`` envelopes and any browser error.

    A read-only visit must leave the stored state as it found it -- neither losing the seed's
    records nor adding the app's own -- and an empty page or a bare loading screen means the app
    never rendered the seed. Five more first-view faults fail an app without a model: an opening
    that reads as a failure, template or leaked-value text, an image the browser cannot draw, a
    page with nothing to click, and a page that shows too little of the company's world
    (:func:`content_floor`, from the SEEN of TOTAL seeded strings the caller counted).
    """
    loading = is_loading(text)
    unchanged = after is not None and stored(before) == stored(after)
    lost = None if unchanged else losses(stored(before), stored(after))
    gained = None if unchanged else added_records(stored(before), stored(after))
    thin = content_floor(seen, total, len(text), dom)
    failure = failure_text(text)
    placeholders = placeholder_text(text)
    images = broken_images(dom) if dom is not None else []
    blanks = empty_images(dom) if dom is not None else 0
    affordances = controls(dom) if dom is not None else None
    if error is None:
        if not text:
            error = "no visible text"
        elif loading:
            error = f"loading screen: {text[:80]!r}"
        elif failure:
            error = f"the first view opens on a failure: {failure!r}"
        elif placeholders:
            error = "template or leaked text in the page: " + ", ".join(repr(p) for p in placeholders)
        elif images and BLOCK_BROKEN_IMAGES:
            error = f"{len(images)} image(s) the browser cannot draw: " + ", ".join(images[:3])
        elif affordances == 0 and len(dom or "") > APP_DOM:
            error = "nothing on the page can be clicked"
        elif thin:
            error = thin
        elif after is None:
            error = "stored_state could not be read after the visit"
        elif lost:
            error = "a read-only visit lost seeded data: " + "; ".join(lost)
        elif gained:
            error = "a read-only visit added the app's own records: " + "; ".join(gained)
    return {
        "ok": error is None,
        "loading": loading,
        "text_chars": len(text),
        "controls": affordances,
        "broken_images": images,
        "empty_images": blanks,
        "placeholders": placeholders,
        "state_unchanged": unchanged,
        "state_change": None if unchanged or after is None else change_summary(stored(before), stored(after)),
        "state_added": gained or [],
        "error": error,
    }


def losses(before, after):
    """What a visit destroyed: top-level keys that vanished and collections that lost records.

    An app that fills in a missing settings block, or reshapes a field, changes the state
    without losing anything; that is recorded as ``state_change`` and does not fail the app.
    Records that disappear do fail it: that is the demo-state wipe this check exists to catch.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["stored_state is no longer an object"] if isinstance(before, dict) else []
    found = [f"top-level key {k!r} vanished" for k in before if k not in after]
    for key, value in before.items():
        if key not in after or not isinstance(value, (list, dict)):
            continue
        ids = _record_ids(value)
        kept = _record_ids(after[key])
        if ids and len(ids - kept) > 0:
            found.append(f"{key} lost {len(ids - kept)} of {len(ids)} records")
        elif not ids and isinstance(value, list) and len(after[key]) < len(value):
            found.append(f"{key} shrank from {len(value)} to {len(after[key])} entries")
    return found


def added_records(before, after):
    """Records the app wrote into the world that the seed never had: its own demo data.

    :func:`losses` only ever looked at keys that vanished, so the opposite went through: ``clio``
    passed with ``state_change: "+trustAccounts +trustTransactions +onlinePayments
    +appIntegrations"``, which is the clone hydrating its shipped defaults over the seed -- three
    trust accounts at the Royal Bank of Canada and six transactions naming another firm's matters
    ("Grey v. Thompson", "Martinez divorce"), written into a law firm's world by a read-only
    visit. That is the demo-data contamination this check exists to catch.

    A key the app fills in that carries no records is not that: ``gmail`` adds ``settings`` in all
    20 companies, ``facebook`` adds three empty lists, ``hubspot`` an empty ``emails``. Only a new
    key that arrives holding record rows fails an app, and the message names the key and the count
    so the repair -- seeding the key, or patching the clone's default -- has something to act on.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    found = []
    for key, value in after.items():
        if key in before:
            continue
        rows = _records_in(value)
        if rows:
            found.append(f"{key} arrived holding {rows} record(s) the seed never had")
    return found


def _records_in(value, depth=0):
    """Record rows anywhere in a value: a dict carrying an ``id``, however deeply nested."""
    if depth > 4:
        return 0
    if isinstance(value, dict):
        return 1 if "id" in value else sum(_records_in(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return sum(_records_in(item, depth + 1) for item in value)
    return 0


def _record_ids(collection):
    rows = (
        collection
        if isinstance(collection, list)
        else list(collection.values())
        if isinstance(collection, dict)
        else []
    )
    return {str(r["id"]) for r in rows if isinstance(r, dict) and "id" in r}


def change_summary(before, after):
    """A short account of a non-destructive change: keys added, collections and fields touched."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "state shape changed"
    parts = [f"+{k}" for k in after if k not in before]
    for key in before:
        if key in after and before[key] != after[key]:
            fields = set()
            b_rows, a_rows = before[key], after[key]
            if isinstance(b_rows, list) and isinstance(a_rows, list):
                for x, y in zip(b_rows, a_rows):
                    if isinstance(x, dict) and isinstance(y, dict):
                        fields |= {f for f in set(x) | set(y) if x.get(f) != y.get(f)}
            parts.append(f"~{key}" + (f"[{','.join(sorted(fields)[:6])}]" if fields else ""))
    return " ".join(parts[:12])


# Where an app keeps the company's world. Both gates only ever opened ``/``, and for several
# apps ``/`` is a launcher: sheets' ``/`` lists "Recent spreadsheets" whose only seeded string is
# the title, while the grid with the company's data and its tabs is one click away. Measured on
# one real company world each, exact matches, the same render this gate does:
#
#   google_sheets_mock  /  1/400 strings,  199 chars -> /spreadsheet  54/400,  2489 chars
#   google_drive_mock   /  1/400 strings,  217 chars -> /recent       21/400,  1322 chars
#   hubspot_mock        /  2/400 strings, 1288 chars -> /contacts     24/400,  2461 chars
#
# Drive's ``/`` is not broken -- it is My Drive, and every file sits inside one folder -- and
# hubspot's ``/`` is a dashboard of totals. Both are honest screens that show a worker none of
# the records they came for; ``/recent`` and ``/contacts`` are where those records are.
#
# One route per app, because the gate, the VM launcher and the worker's start page must open the
# same page. This table is the gate's fallback, not the home of the fact: the route belongs in
# ``catalogs/apps.json`` (an ``entry_path`` per app, built by ``scripts/build_apps.py``) and in
# the proxy URL ``hub_world`` writes into ``runtime/endpoints.json``, which is the one string
# ``hub_vm`` already turns into the start-page tile, the bookmark and the startup tab. An
# ``entry_path`` on the endpoint, or a path already in the URL, wins over this table.
# Loaded from catalogs/app_entry_routes.json, which is the upstream source the VM launcher and the
# worker's start page read too. The literals remain as the fallback for a checkout without the file.
from .hub_app import entry_routes

ENTRY_ROUTES = {
    "google_sheets_mock": "/spreadsheet",
    "google_drive_mock": "/recent",
    "hubspot_mock": "/contacts",
} | entry_routes()


def entry_route(entry, app_id=None, path="/"):
    """The route this app lands a worker on: the endpoint's own record, the URL's, else the table."""
    recorded = (entry or {}).get("entry_path")
    if isinstance(recorded, str) and recorded.startswith("/"):
        return recorded
    if path and path != "/":
        return path
    return ENTRY_ROUTES.get(app_id, "/")


def worker_url(entry, host=None, app_id=None):
    """The first worker's proxy URL and its ``/state`` URL, reachable from this host.

    ``hub-serve --host 0.0.0.0`` records the bind address; a browser cannot visit 0.0.0.0, so it
    becomes 127.0.0.1 unless HOST names the interface to use. The path is the app's entry route
    (:func:`entry_route`), so the gate opens the page the worker will open.
    """
    url = next(iter(entry["workers"].values()))
    parts = urlsplit(url)
    target = host or ("127.0.0.1" if parts.hostname == "0.0.0.0" else parts.hostname)
    netloc = f"{target}:{parts.port}" if parts.port else target
    sid = parse_qs(parts.query).get("sid", [""])[0]
    page = urlunsplit((parts.scheme, netloc, entry_route(entry, app_id, parts.path), parts.query, ""))
    state = urlunsplit((parts.scheme, netloc, "/state", f"sid={sid}", ""))
    return page, state


def fetch_json(url, timeout=20):
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def render_dom(
    browser, url, *, profile, timeout=BROWSER_TIMEOUT, virtual_time_ms=VIRTUAL_TIME_MS, screenshot=None
):
    """``--dump-dom`` of URL through a throwaway profile: the first visit from a new VM browser.

    With SCREENSHOT the same visit also writes that PNG, so the picture and the text describe one
    render. Returns (dom, error); a non-zero exit, a timeout or a browser that will not start is
    an error.
    """
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--window-size=1600,1000",  # a wider pane than a laptop: fewer labels clipped to fit
        f"--virtual-time-budget={virtual_time_ms}",
        "--run-all-compositor-stages-before-draw",
        f"--user-data-dir={profile}",
        *([f"--screenshot={screenshot}"] if screenshot else []),
        "--dump-dom",
        url,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return "", f"browser timed out after {timeout}s"
    except OSError as exc:
        return "", f"browser could not start: {exc}"
    if result.returncode:
        tail = [line for line in result.stderr.splitlines() if line.strip()]
        detail = f": {tail[-1].strip()[:200]}" if tail else ""
        return result.stdout, f"browser exited {result.returncode}{detail}"
    return result.stdout, None


SHOT_WIDTH = 1280
SHOT_QUALITY = 60


def keep_screenshot(raw, target):
    """Shrink the render's PNG into TARGET as JPEG; the original is thrown away with the profile.

    A screenshot is evidence for a person and for the visual judge, not an archive: a page-wide
    JPEG is a tenth of the PNG and reads the same. Without Pillow the PNG is copied as it is.
    """
    raw, target = Path(raw), Path(target)
    if not raw.is_file():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        with Image.open(raw) as image:
            image = image.convert("RGB")
            if image.width > SHOT_WIDTH:
                image.thumbnail((SHOT_WIDTH, image.height * SHOT_WIDTH // image.width))
            image.save(target, "JPEG", quality=SHOT_QUALITY, optimize=True)
    except (ImportError, OSError, ValueError):
        target = target.with_suffix(".png")
        shutil.copyfile(raw, target)
    return target


def check_app(
    folder,
    entry,
    *,
    browser,
    app_id=None,
    host=None,
    work=None,
    settle=SETTLE_SECONDS,
    render=None,
    fetch=None,
    shot=None,
):
    """Read the state, render the page, wait, read the state again; the verdict plus what was seen."""
    render, fetch = render or render_dom, fetch or fetch_json
    page, state_url = worker_url(entry, host, app_id)
    state_path = Path(folder) / entry["state_file"]
    strings = seeded_strings(read(state_path)) if state_path.is_file() else []
    seen = f"0/{len(strings)}"
    try:
        before = fetch(state_url)
    except (OSError, ValueError) as exc:
        return {**verdict("", None, None, error=f"state fetch failed: {exc}"), "seeded_strings_seen": seen}
    if work:
        Path(work).mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="render-profile-", dir=work)
    raw = Path(profile) / "screen.png" if shot else None
    try:
        dom, error = render(browser, page, profile=profile, screenshot=raw)
        picture = keep_screenshot(raw, shot) if shot else None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    text = visible_text(dom or "")
    time.sleep(settle)
    try:
        after = fetch(state_url)
    except (OSError, ValueError) as exc:
        after, error = None, error or f"state fetch failed: {exc}"
    shown = strings_seen(strings, text)
    result = verdict(text, before, after, error=error, dom=dom, seen=shown, total=len(strings))
    result["seeded_strings_seen"] = f"{shown}/{len(strings)}"
    result["route"] = urlsplit(page).path or "/"
    result["screenshot"] = str(picture.relative_to(Path(folder))) if picture else None
    return result


def check_folder(folder, *, host=None, work=None, browser=None, settle=SETTLE_SECONDS, **options):
    """Render every app in ``runtime/endpoints.json``; the RENDER.json content (not yet written).

    Without a browser the result is skipped and ok: a host without Chrome must not stop the
    pipeline; the VM stage still exercises the apps in a real browser.

    The report publishes the waits this render used and the one a guest needs, so the VM launcher
    and the guest screenshot step can hold the same page still for as long as this gate did.
    """
    folder = Path(folder)
    endpoints = read(folder / "runtime" / "endpoints.json")["apps"]
    browser = browser or find_browser()
    report = {
        "ok": True,
        "at": now(),
        # Which code reached this verdict. Recorded here rather than in run_render_check so that the
        # report a reader computes and the report on disk are the same object, including the skipped
        # and empty returns below -- a marker written by either of those still has to say what
        # decided it, or the one kind of render that proves nothing would also be the one kind that
        # never reads as stale.
        "decided_by": decided_by(DECIDES_THIS),
        "browser": browser,
        "settle_seconds": settle,
        "virtual_time_ms": VIRTUAL_TIME_MS,
        "guest_settle_seconds": settle * GUEST_SLOWDOWN,
        "apps": {},
    }
    if browser is None:
        # A host that cannot render is a fault about the *environment*: it says nothing whatever
        # about this world, which is exactly what ``faulted`` means. It is not ``unmeasured`` -- that
        # word is for inputs that were not there, and the apps were -- and the difference matters
        # because the two want opposite handling: see ``admissible`` below.
        report.update(Receipt.faulted(f"this host cannot render: {NO_BROWSER}").body, skipped=NO_BROWSER)
        return report
    shots = folder / SHOT_DIR
    for app_id in sorted(endpoints):
        result = check_app(
            folder,
            endpoints[app_id],
            browser=browser,
            app_id=app_id,
            host=host,
            work=work,
            settle=settle,
            shot=shots / f"{app_id}.jpg",
            **options,
        )
        report["apps"][app_id] = result
        report["ok"] = report["ok"] and result["ok"]
    # A render of no apps is not a render that passed, and ``ok`` now says so: it is derived from the
    # outcome rather than seeded True and ANDed down, which for every render that has ever happened
    # is the same value (all apps ok -> True, any app failed -> False) and for the empty case is None
    # instead of True. ``rollup`` is where "an empty sequence is unmeasured" lives for every stage.
    whole = rollup(list(report["apps"].values()), "rendered apps")
    report.update(whole.body, ok=OUTCOME_OK[whole.outcome])
    return report


# What decides a render verdict, and the reason this list is longer than this file. The browser is
# handed a page the proxy builds, so the verdict is the proxy's as much as this module's: hub_app.py
# carries the storage shim that stopped 76 of 88 apps falling back to their demo rows at real volume,
# hub_identity.py decides which person the page is served as, and placeholder_images.py decides
# whether an image draws at all -- every one of them a thing this gate reports on and none of them in
# this file. PROXY_CODE omitted hub_app.py, which is why that shim could never restale a RENDER.json.
DECIDES_THIS = (
    Path(__file__),
    Path(__file__).with_name("hub_app.py"),
    Path(__file__).with_name("hub_identity.py"),
    Path(__file__).with_name("placeholder_images.py"),
)


def current(marker):
    """Whether a RENDER.json was decided by the code that renders as it stands now.

    The driver's accept slot, bulk_layer.current's contract. A marker with no ``decided_by`` is
    stale, which is the migration. Note what this does *not* answer: ``current`` is about whether the
    verdict is still this code's, and ``admissible`` below is about whether the verdict lets a
    company go on. The driver's row needs both, because the accept slot is a re-run trigger -- a
    marker it refuses makes the step run again -- so a stale digest means "render it again" while an
    unmeasured render means "this company is not ready", and only one of the two is about the code.

    That distinction is why a render is one of the few verdicts an accept slot should hold at all:
    re-rendering is minutes of CPU and no model call, so "run it again" is a real remedy. It is not
    for the expensive or the unrepairable -- ``passed`` on SEED.json would have re-seeded the 50 of 60
    worlds whose review failed, about 1,650 core-hours over worlds that already exist. See
    ``company_envs.receipt`` for the rest of that measurement.
    """
    return isinstance(marker, dict) and marker.get("decided_by") == decided_by(DECIDES_THIS)


def admissible(report):
    """Whether a RENDER.json lets a company go on -- which is not the same as having passed.

    Three answers and two of them advance, and the rule is stated here rather than in each caller
    because it is a fact about what a render is, not a judgement about a particular fleet's hosts.

    ``passed``  every served app rendered.
    ``faulted`` with a recorded skip -- this host cannot render at all. No stage can install a
                browser, so blocking would be a permanent stall on a defect the pipeline cannot
                repair; the VM stage takes this measurement again in a real browser. Admitted, and
                the receipt says plainly that nothing about the world was learned.
    ``unmeasured`` apps were expected and none was rendered. That is a company nobody has looked at
                and it must not reach the VM stage, so it is refused. It is a latent hole rather
                than a live one: measured 2026-09-10, 0 of the 20 RENDER.json on disk rendered zero
                apps and 0 of the 51 endpoints.json held zero apps.

    "A fault must be retried" holds for a fault the environment can clear. This one it cannot, which
    is why the rule names it instead of reading ``ok``.
    """
    return outcome_of(report) == PASSED or (faulted(report) and bool(report.get("skipped")))


def run_render_check(folder, *, host=None, work=None, **options):
    """Check the folder's served apps and write ``runtime/RENDER.json``; returns the report."""
    folder = Path(folder)
    report = check_folder(folder, host=host, work=work, **options)
    write(folder / "runtime" / "RENDER.json", report)
    return report


def format_lines(report):
    """One line per app (or the skip notice), then the overall verdict."""
    if report.get("skipped"):
        return [f"SKIP render-check: {report['skipped']}"]
    lines = []
    for app_id, result in report["apps"].items():
        state = "unchanged" if result["state_unchanged"] else "CHANGED"
        line = (
            f"{'PASS' if result['ok'] else 'FAIL'} {app_id}: {result['text_chars']} visible chars, "
            f"seeded strings seen {result['seeded_strings_seen']}, state {state}"
            + (f", route {route}" if (route := result.get("route", "/")) != "/" else "")
        )
        lines.append(line + (f" -- {result['error']}" if result["error"] else ""))
    failed = [app for app, result in report["apps"].items() if not result["ok"]]
    lines.append(
        f"render-check {'ok' if report['ok'] else 'FAILED'}: {len(report['apps'])} apps"
        + (f", failed {', '.join(failed)}" if failed else "")
    )
    return lines
