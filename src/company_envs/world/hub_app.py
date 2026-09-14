"""Generic CUA-Gym hub adapter: one state contract for every hub mock application.

Every hub app implements the same server protocol from its own vite config:
``POST /post {action: set|set_current|reset}``, ``GET /state`` and ``GET /go``.
Seeding therefore means posting the full native state once per session id; no
per-app Python store is needed. Identity is ``shared_session``: all workers of
one company world share one session id per app, and attribution comes from the
worker's own VM trace, not from an in-app login the hub does not have.
"""

import hashlib
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import threading
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen

from . import placeholder_images as placeholders

HUB_SOURCE = "cua_gym_hub"
SOURCE_EXCLUDES = {
    ".git",
    ".hub-serving",  # a live server's claim on its own directory; a copy inherits no such claim
    ".mock-states",
    ".mock-files",
    ".vite",
    "node_modules",
    "dist",
    "build",
    ".hub-build.json",
}
# /state is the app's own first-visit load path (fetchCustomState), so workers need it.
# It reveals only the current state the UI already shows; /go (baseline + diff) stays private.
HARNESS_READS = {"/go"}
SID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
PATCHES_PATH = Path(__file__).parent / "hub_patches.json"


def patches(app_id):
    """Tiny recorded source edits for upstream hub defects; empty for most apps.

    Each edit is an exact once-occurring old/new string in one file. They are hashed into
    the build marker so a changed patch rebuilds, and reported in every smoke record.
    """
    if not PATCHES_PATH.is_file():
        return []
    return json.loads(PATCHES_PATH.read_text()).get(app_id, [])


def apply_patches(target, app_id):
    applied = []
    for edit in patches(app_id):
        path = Path(target) / edit["file"]
        text = path.read_text()
        if text.count(edit["old"]) != 1:
            raise ValueError(f"Patch for {app_id} does not match exactly once: {edit['file']}")
        path.write_text(text.replace(edit["old"], edit["new"]))
        applied.append({"file": edit["file"], "reason": edit.get("reason", "")})
    return applied


# A company's real records are far larger than the few demo rows these apps ship with, and every
# one of them caches state in localStorage, whose quota is about 5 MB per origin. An app that
# writes the state twice -- once as the current state, once as the baseline -- needs twice the
# room, so a 2.5 MB seed already overflows. The throw escapes the app's own hydration and it falls
# back to its shipped demo data, which looks like a working app holding somebody else's records.
# The server holds the authoritative state and every app re-reads it on load, so a cache write
# that cannot fit is not a failure worth stopping for: it is recorded and skipped.
STORAGE_SHIM = """<script>
(function () {
  var set = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    try {
      return set.call(this, key, value);
    } catch (error) {
      window.__hubStorageOverflow = (window.__hubStorageOverflow || 0) + 1;
    }
  };
})();
</script>
"""


# writeState returns false when the write does not land, and 255 call sites across 82 apps call it
# as a bare statement and then answer {"success": true} regardless. A world whose session directory
# has gone therefore goes on reporting that it was seeded while serving the demo records the app
# ships with -- cloudflare_mock answered success with 32,763 bytes of demo data where 5,120,814
# bytes of company records had been, and named that as both initial and current. A discarded
# failure becomes a raised one: loud beats silent, and the harness can see it.
# The amended write's message, named once: it is both the marker that an app is already
# amended and part of the build key, so the two must never drift apart.
ASSERT_MESSAGE = "state write did not land"
# A trailing semicolon is still a discarded result; four configs write it that way.
BARE_WRITE = re.compile(r"^([ \t]*)writeState\s*\(([^;\n]*)\)[ \t]*;?[ \t]*$", re.MULTILINE)


def _signals_by_return(text, name="writeState"):
    """Whether this function reports failure by returning, rather than by letting it propagate.

    Brace-matched rather than matched by pattern: the usual shape puts the return inside a try
    block, and a regex that allows one level of nesting cannot see it -- which skipped all 88 apps
    when tried.
    """
    start = text.find(f"function {name}")
    if start < 0:
        return False
    opened = text.find("{", start)
    if opened < 0:
        return False
    depth = 0
    for index in range(opened, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return "return" in text[opened:index]
    return False


def assert_state_writes(target):
    """Make a discarded write failure raise, in one app's server. Returns the sites amended.

    Only where ``writeState`` signals failure by returning false. Two apps let the filesystem error
    propagate instead and return nothing -- their failures were already loud, and wrapping them in
    ``if (!writeState(...))`` made *every* write throw, since ``!undefined`` is true. That turned
    their /post into a 400 and broke both apps outright.
    """
    amended = 0
    for name in ("vite.config.js", "vite.config.ts"):
        path = Path(target) / name
        if not path.is_file():
            continue
        text = path.read_text()
        if ASSERT_MESSAGE in text or not _signals_by_return(text):
            continue
        # A throw needs no `res` in scope and is valid wherever the statement stood, so one
        # amendment fits every call site without reading each handler.
        patched, count = BARE_WRITE.subn(
            lambda m: (
                f"{m.group(1)}if (!writeState({m.group(2)})) "
                f"throw new Error('{ASSERT_MESSAGE}')"
            ),
            text,
        )
        if count:
            path.write_text(patched)
            amended += count
    return amended


def inject_storage_shim(target):
    """Make an over-quota cache write a no-op in one app, before any of its own script runs."""
    path = Path(target) / "index.html"
    text = path.read_text()
    if "__hubStorageOverflow" in text:
        return False
    # Ordering is the whole point, so anchor on whichever of these the page has: the shim must
    # already be installed by the time the app's first script asks the browser to store anything.
    for anchor, replacement in (("<head>", "<head>\n" + STORAGE_SHIM), ("<script", STORAGE_SHIM + "<script")):
        if anchor in text:
            path.write_text(text.replace(anchor, replacement, 1))
            return True
    path.write_text(STORAGE_SHIM + text)
    return True


def hub_apps(catalog_apps):
    """Catalog entries that carry the hub contract and a pinned schema document."""
    return {
        app["id"]: app
        for app in catalog_apps
        if app.get("source") == HUB_SOURCE and app.get("schema") and app.get("state_keys")
    }


def validate_sid(sid):
    if not isinstance(sid, str) or not SID_PATTERN.fullmatch(sid):
        raise ValueError("Session id must be 1-64 characters of [A-Za-z0-9_-]")
    return sid


def top_level_keys(schema_text):
    """Top-level state keys from the schema document's ``## State Schema`` table, or None."""
    lines = schema_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().lower().startswith("## state schema"))
    except StopIteration:
        return None
    keys = []
    for line in lines[start + 1 :]:
        # The first table after the heading is the top-level contract; some schemas put
        # it under a "### Top-Level Keys" subheading, so stop only once a table has ended.
        if line.startswith("#") and keys:
            break
        match = re.match(r"\|\s*`([^`]+)`\s*\|", line)
        if match:
            # Some tables list nested paths (`contracts[].parties[]`) under the top-level rows;
            # a path is documentation of a record's shape, not a key the state has.
            if not re.search(r"[\[\].]", match.group(1)):
                keys.append(match.group(1))
        elif keys and not line.strip().startswith("|"):
            break
    return keys or None


# The schema documents are imported upstream documentation, pinned by hash in manifest.json, so
# they are not ours to correct. But a key the app keeps and renders, which the document omits, can
# never be seeded: validate_state refuses it, so the app shows the demo records it ships with and
# writes those into the company's world. The supplement records those keys -- observed by serving
# each app and reading the state it keeps -- the way hub_patches.json records source amendments.
# They are PERMITTED in a seed, never required: a world that predates them still validates. A key
# becomes required by being documented upstream, once the seeding skill fills it.
SUPPLEMENT_PATH = Path(__file__).parent.parent.parent.parent / "catalogs/app_schema_supplement.json"


def schema_supplement(path=None):
    """Per-app keys the app was observed to keep that its imported schema omits."""
    path = Path(path) if path else SUPPLEMENT_PATH
    if not path.is_file():
        return {}
    return json.loads(path.read_text()).get("apps", {})


ENTRY_ROUTES_PATH = Path(__file__).parent.parent.parent.parent / "catalogs/app_entry_routes.json"


def entry_routes(path=None):
    """The page each app lands a worker on, by app id; empty for an app that opens ``/``.

    The gate, the VM launcher and the worker's start page have to open the same page, so this is the
    one upstream source. It is a file of ours rather than a column in ``apps.json`` because that
    catalogue is hashed by ``manifest.json`` as imported provenance -- editing it registers as an
    integrity error, the same way editing the imported schema documents did.
    """
    path = Path(path) if path else ENTRY_ROUTES_PATH
    if not path.is_file():
        return {}
    return {
        app_id: entry["entry_path"]
        for app_id, entry in (json.loads(path.read_text()).get("apps") or {}).items()
        if isinstance(entry.get("entry_path"), str) and entry["entry_path"].startswith("/")
    }


def permitted_keys(app_id, schema_text, supplement=None):
    """Every key a seed may carry: the imported contract plus the observed supplement."""
    declared = top_level_keys(schema_text)
    if declared is None:
        return None
    entry = (supplement if supplement is not None else schema_supplement()).get(app_id) or {}
    extra = [key for key in (entry.get("keys") or {}) if key not in declared]
    return declared + extra


UI_STATE_KEYS = {
    "currentUser",
    "user",
    "settings",
    "ui",
    "view",
    "viewMode",
    "sortConfig",
    "zoom",
    "selectedItems",
    "selectionRange",
    "clipboard",
    "undoStack",
    "redoStack",
    "activeSheetId",
    "currentDate",
    "sidebarOpen",
    "favorites",
    "shoppingCart",
    "uploadQueue",
    "storageUsed",
    "storageTotal",
    "navigatorFilter",
    "activeModule",
    "currentSortColumn",
    "currentSortDirection",
    "currentListFilters",
    "navigatorExpandedSections",
    "callHistory",
    "bookmarkedMessages",
    "isDragging",
    "showGridlines",
    "showFormulas",
    "id",
    "title",
    "workspace",
    "invitations",
    "notifications",
    "theme",
    "filters",
    "searchQuery",
    "activeView",
    "currentView",
    "selectedId",
    "selected",
    "expanded",
    "modal",
    "loading",
}


SCALAR_TYPES = {
    "string",
    "number",
    "boolean",
    "bool",
    "int",
    "integer",
    "float",
    "date",
    "null",
    "undefined",
    "any",
    "unknown",
}


def element_type(kind):
    """The element type of a declared collection type, or None when the type is not a collection.

    ``array`` and ``object`` are the house spellings, and 20 of the 94 pinned schemas write the
    TypeScript type instead -- ``Issue[]``, ``Record<string, Board>``, ``{[targetId: string]:
    Comment[]}``. Those 20 (salesforce, ServiceNow, jira, airtable, confluence, workday, ...) used
    to classify as no collection at all, so they got no bulk layer and no entry in BULK.json saying
    why, and the feature matrix fell back to every top-level key.
    """
    text = kind.strip().strip("`").replace("\\", "").strip()
    for pattern in (
        r"(.+?)\s*\[\]",  # Issue[]
        r"record\s*<\s*[^,<>]+,\s*(.+?)\s*>",  # Record<string, Board>
        r"\{\s*\[[^\]]*\]\s*:\s*(.+?)\s*\}",  # {[targetId: string]: Comment[]}
    ):
        if match := re.fullmatch(pattern, text, re.IGNORECASE):
            return match.group(1).strip() or "any"
    return None


def record_collections(schema_text):
    """Top-level keys that hold the app's records: arrays and keyed maps, never UI state.

    A feature cell names a decisive record collection. Drawn from every key, the matrix handed
    authors cells like ``google_drive_mock.selectedItems`` and ``google_sheets_mock.zoom``.

    A TypeScript element type counts, a scalar one does not: ``Issue[]`` is a collection of
    records, ``string[]`` is a list of ids or labels and ``{[answerId: string]: boolean}`` a set of
    flags, neither of which anything can seed records into.
    """
    lines = schema_text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().lower().startswith("## state schema"))
    except StopIteration:
        return None
    keys = []
    for line in lines[start + 1 :]:
        if line.startswith("#") and keys:
            break
        match = re.match(r"\|\s*`([^`]+)`\s*\|\s*([^|]*)\|\s*([^|]*)\|", line)
        if match:
            name, kind, description = match.group(1), match.group(2).strip().lower(), match.group(3).lower()
            if re.search(r"[\[\].]", name) or name in UI_STATE_KEYS:
                continue
            keyed_map = kind.startswith("object") and re.search(
                r"map|keyed|dictionary|→|->|by id|by channel|record", description
            )
            element = element_type(kind)
            typed = element is not None and element.removesuffix("[]").strip() not in SCALAR_TYPES
            if kind.startswith("array") or keyed_map or typed:
                keys.append(name)
        elif keys and not line.strip().startswith("|"):
            break
    return keys or None


def validate_state(app, state, schema_text=None):
    """The seed is the whole native state: every documented top-level key, nothing undocumented.

    ``state_keys`` in the catalog is a flattened name list (top-level and nested), so the
    exact top-level contract comes from the schema document when it is supplied.
    """
    if not isinstance(state, dict) or not state:
        raise TypeError("Hub app state must be a nonempty JSON object")
    declared = top_level_keys(schema_text) if schema_text else None
    if declared is None:
        if unknown := state.keys() - set(app["state_keys"]):
            raise ValueError(
                f"{app['id']} state has keys absent from the documented state: {sorted(unknown)}"
            )
    else:
        if missing := set(declared) - state.keys():
            raise ValueError(f"{app['id']} state is missing documented keys: {sorted(missing)}")
        if extra := state.keys() - set(permitted_keys(app["id"], schema_text) or declared):
            raise ValueError(f"{app['id']} state has undocumented keys: {sorted(extra)}")
    if check := NESTED_STATE_CHECKS.get(app["id"]):
        # Top-level keys are the contract; a few apps also need nested shapes the frontend
        # reads before its first render, or the seeded app never leaves its loading screen.
        check(state)
    return state


def _pill_shelf(value):
    return isinstance(value, list) and all(
        isinstance(p, dict) and isinstance(p.get("fieldName"), str) for p in value
    )


def validate_tableau_state(state):
    """Nested shapes the patched tableau_mock frontend reads while it builds the workbook view.

    The patch (hub_patches.json, dataManager.js) maps each worksheet's ``chartData`` to a chart
    config before the first render; a shape it cannot read throws inside the load promise and
    the app stays on "Loading" forever. Both documented forms pass: the schema's
    ``{type, categories[], series[{name, values[]}]}`` object and a list of row objects.
    """
    problems = []
    workbook = state.get("workbook")
    if not isinstance(workbook, dict) or not isinstance(workbook.get("id"), str) or not workbook["id"]:
        problems.append("workbook must be an object with a non-empty string id")
    else:
        if not isinstance(workbook.get("name"), str):
            problems.append("workbook.name must be a string")
        if not isinstance(workbook.get("sheetOrder"), list):
            problems.append("workbook.sheetOrder must be a list of sheet ids")
    user = state.get("currentUser")
    if not isinstance(user, dict) or not isinstance(user.get("name"), str):
        problems.append("currentUser must be an object with a string name")
    if not isinstance(state.get("uiState"), dict):
        problems.append("uiState must be an object")
    worksheets = state.get("worksheets")
    if not isinstance(worksheets, list):
        problems.append("worksheets must be a list")
        worksheets = []
    for index, sheet in enumerate(worksheets):
        if not isinstance(sheet, dict):
            problems.append(f"worksheets[{index}] must be an object")
            continue
        label = f"worksheets[{index}]" + (f" ({sheet['id']})" if isinstance(sheet.get("id"), str) else "")
        if not isinstance(sheet.get("id"), str) or not sheet["id"]:
            problems.append(f"{label} needs a non-empty string id")
        for shelf in ("columns", "rows"):
            if not _pill_shelf(sheet.get(shelf, [])):
                problems.append(f"{label}.{shelf} must be a list of pills, each with a fieldName")
        chart = sheet.get("chartData")
        if isinstance(chart, list):
            if not all(isinstance(row, dict) for row in chart):
                problems.append(f"{label}.chartData rows must all be objects of field: value")
        elif isinstance(chart, dict):
            series = chart.get("series")
            if not isinstance(chart.get("categories"), list) or not isinstance(series, list):
                problems.append(f"{label}.chartData object needs categories[] and series[]")
            elif not all(
                isinstance(s, dict) and isinstance(s.get("name"), str) and isinstance(s.get("values"), list)
                for s in series
            ):
                problems.append(f"{label}.chartData.series entries must be {{name, values[]}}")
        else:
            problems.append(
                f"{label}.chartData must be an object {{type, categories, series}} or a list of row objects"
            )
    if problems:
        raise ValueError(
            "tableau_mock state would not render (the app would stay on Loading): " + "; ".join(problems)
        )


NESTED_STATE_CHECKS = {"tableau_mock": validate_tableau_state}


def probe_key(app, state):
    """A top-level key of the seeded state to overwrite for the diff probe.

    The catalog's ``state_keys`` is a flattened name list that can start with a nested
    or mis-parsed name (outlook_web_mock lists ``inbox`` first), so prefer the first
    catalog name that is really a top-level key and fall back to the state's own first key.
    """
    return next((k for k in app["state_keys"] if k in state), next(iter(state)))


def render_check(url, expect, seconds=8):
    """Headless-Chrome DOM check that the seeded app actually shows seeded content.

    Returns None when no Chrome is installed; the protocol checks still stand alone.
    """
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        return None
    result = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            f"--virtual-time-budget={seconds * 1000}",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=seconds * 4,
        check=False,
    )
    return expect in result.stdout


def source_hash(source):
    """Hash the app source tree, excluding installed and generated files."""
    result = hashlib.sha256()
    for parent, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d not in SOURCE_EXCLUDES)
        for directory in dirs:
            if (Path(parent) / directory).is_symlink():
                raise ValueError(f"Symlink is not a supported build input: {Path(parent) / directory}")
        for name in sorted(f for f in files if f not in SOURCE_EXCLUDES):
            path = Path(parent) / name
            if path.is_symlink():
                raise ValueError(f"Symlink is not a supported build input: {path}")
            result.update(str(path.relative_to(source)).encode())
            result.update(path.read_bytes())
    return result.hexdigest()


def refuse_if_serving(target):
    """Never destroy a directory a live server keeps its sessions in."""
    if (held := serving_pid(target)) is not None:
        raise ValueError(
            f"{target} is being served by pid {held}; rebuilding it would delete that server's "
            f"sessions, and the app would go on answering success while serving its own demo data"
        )



def amendment_hash():
    """A digest of the amendments ``build`` injects, so changing one rebuilds every app.

    This used to be two hand-maintained integers, and a hand-maintained integer is a promise to
    remember. ``source_hash`` covers the app's own source but not the text hub_app adds to it, so
    an edited shim or a widened write assertion left every cached build valid and the fix reached
    nobody -- the stale-marker class, one layer down in the build key rather than in a driver
    marker. Digesting the amendment text itself removes the promise.
    """
    return hashlib.sha256(
        (STORAGE_SHIM + BARE_WRITE.pattern + ASSERT_MESSAGE).encode()
    ).hexdigest()


def build(source, target, log=None, timeout=600):
    """Copy, install and build one hub app; reuse the build when the source is unchanged."""
    source, target = Path(source), Path(target)
    if not (source / "vite.config.js").is_file() and not (source / "vite.config.ts").is_file():
        raise ValueError(f"Not a hub vite app: {source}")
    app_id = source.name
    digest = hashlib.sha256(
        (
            source_hash(source)
            + json.dumps(patches(app_id), sort_keys=True)
            + amendment_hash()
        ).encode()
    ).hexdigest()
    marker = target / ".hub-build.json"
    if marker.is_file() and (target / "dist" / "index.html").is_file():
        try:
            cached = json.loads(marker.read_text())
            valid = (
                cached.get("source_hash") == digest
                and cached.get("built_hash") == source_hash(target)
                and cached.get("dist_hash") == source_hash(target / "dist")
            )
        except (ValueError, OSError, AttributeError):
            valid = False
        if valid:
            return target  # nothing is destroyed, so a live server may keep serving it
        refuse_if_serving(target)
        shutil.rmtree(target)
    refuse_if_serving(target)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*SOURCE_EXCLUDES))
    applied = apply_patches(target, app_id)
    assert_state_writes(target)
    stream = open(log, "w") if log else subprocess.DEVNULL  # noqa: SIM115 - closed below
    try:
        install = ["npm", "ci"] if (target / "package-lock.json").is_file() else ["npm", "install"]
        subprocess.run(
            [*install, "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=target,
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        subprocess.run(
            ["node", "node_modules/vite/bin/vite.js", "build"],
            cwd=target,
            check=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    finally:
        if log:
            stream.close()
    if not (target / "dist" / "index.html").is_file():
        raise ValueError("Build produced no dist/index.html")
    # The served page, not the source entry: vite preview hands out dist, and the shim has to be
    # in what the browser actually loads.
    inject_storage_shim(target / "dist")
    marker.write_text(
        json.dumps(
            {
                "source_hash": digest,
                "source": str(source),
                "patches": applied,
                "built_hash": source_hash(target),
                "dist_hash": source_hash(target / "dist"),
            }
        )
    )
    return target


def free_port(host="127.0.0.1"):
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class HubClient:
    """Harness-side protocol client; the same calls work against every hub app."""

    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _json(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or b"null")

    def seed(self, sid, state):
        """Set both the current state and the diff baseline."""
        return self._json("POST", f"/post?sid={validate_sid(sid)}", {"action": "set", "state": state})

    def update(self, sid, state):
        """Change the current state only, as the app itself does after a UI action."""
        return self._json("POST", f"/post?sid={validate_sid(sid)}", {"action": "set_current", "state": state})

    def reset(self, sid):
        return self._json("POST", f"/post?sid={validate_sid(sid)}", {"action": "reset"})

    def current(self, sid):
        return self._json("GET", f"/state?sid={validate_sid(sid)}")

    def inspect(self, sid):
        return self._json("GET", f"/go?sid={validate_sid(sid)}")

    def wait_ready(self, seconds=30):
        deadline = time.monotonic() + seconds
        while True:
            try:
                self._json("GET", "/state?sid=probe")
                return
            except (OSError, ValueError, HTTPError):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Hub app did not answer at {self.base_url}") from None
                time.sleep(0.25)


SERVING_MARKER = ".hub-serving"


def serving_pid(built):
    """The pid of a live server holding this build directory, or None.

    A build directory is not shareable: the app keeps its sessions in ``.mock-states`` inside it,
    HubProcess removes that on stop, and build() removes the whole directory to rebuild. Two live
    servers on one directory therefore delete each other's worlds -- and the app's own /post
    handler ignores the failed write and still answers success, so a wiped world goes on reporting
    that it was seeded while serving the demo records it ships with. batch_queue.prepare_work
    hardlinks a private build per company for exactly this reason; nothing stopped the rest.
    """
    marker = Path(built) / SERVING_MARKER
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None  # the server that wrote it is gone; the marker is stale
    return pid


class HubProcess:
    """A built hub app served by its own vite preview server, loopback only."""

    def __init__(self, built, port=None, log=None):
        self.built = Path(built)
        self.port = port or free_port()
        self.log = log
        self.process = None
        self.owns_marker = False
        self.client = HubClient(f"http://127.0.0.1:{self.port}")

    def ensure_node_modules(self, timeout=600):
        """Reinstall dependencies when a kept ``dist`` build lost its ``node_modules``.

        Builds keep ``dist`` and the build marker while ``node_modules`` may be deleted
        between runs to save disk; vite preview still needs the vite binary.
        """
        vite = self.built / "node_modules" / "vite" / "bin" / "vite.js"
        if vite.is_file():
            return False
        if not (self.built / "dist" / "index.html").is_file():
            raise ValueError(f"Not a built hub app (no dist/index.html): {self.built}")
        flags = ["--ignore-scripts", "--no-audit", "--no-fund"]
        commands = [["npm", "ci", *flags], ["npm", "install", *flags]]
        if not (self.built / "package-lock.json").is_file():
            commands = commands[1:]
        for command in commands:
            result = subprocess.run(
                command, cwd=self.built, check=False, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode == 0 and vite.is_file():
                return True
        raise ValueError(f"npm install did not restore node_modules/vite in {self.built}")

    def start(self):
        if (held := serving_pid(self.built)) is not None:
            raise ValueError(
                f"{self.built} is already being served by pid {held}; serving it twice would "
                f"delete that server's sessions. Use a private build directory per company, as "
                f"batch_queue.prepare_work does."
            )
        self.ensure_node_modules()
        stream = open(self.log, "w") if self.log else subprocess.DEVNULL  # noqa: SIM115 - process-owned
        self.process = subprocess.Popen(
            [
                "node",
                "node_modules/vite/bin/vite.js",
                "preview",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--strictPort",
            ],
            cwd=self.built,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        (self.built / SERVING_MARKER).write_text(str(os.getpid()))
        self.owns_marker = True
        try:
            self.client.wait_ready()
        except TimeoutError:
            self.stop()
            raise
        return self

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if not self.owns_marker:
            return  # another server's sessions live in here; they are not ours to remove
        for name in (".mock-states", ".mock-files"):
            shutil.rmtree(self.built / name, ignore_errors=True)
        (self.built / SERVING_MARKER).unlink(missing_ok=True)
        self.owns_marker = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def worker_allowed(method, path, body=None):
    """Workers get the app and its own saves; seeding, resetting and inspection stay harness-only."""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/") or parsed.fragment:
        return False
    route = parsed.path
    decoded = unquote(route).replace("\\", "/")
    normalized = "/" + posixpath.normpath(decoded).lstrip("/")
    # Connect mounts match /go, /go/... and /go.ext. Reject aliases rather than
    # letting normalization or a mount prefix bypass filtering and identity rewriting.
    for candidate in (route, normalized):
        if re.match(r"^/(?:go|post|state)(?:[/.]|$)", candidate):
            if route != normalized or route not in {"/post", "/state"}:
                return False
            if route == "/state":
                return method in {"GET", "HEAD"}
            return method == "POST" and isinstance(body, dict) and body.get("action") == "set_current"
    return True


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def proxy_urlopen(request, timeout=30):
    # A redirect must return through the worker filter, never through urllib's
    # automatic trusted-side request to another route or another worker's port.
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def proxy_headers(headers):
    excluded = {
        "host",
        "connection",
        "transfer-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
    excluded.update(h.strip().lower() for h in headers.get("Connection", "").split(","))
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def proxy_response_headers(headers):
    allowed = {key.lower() for key in proxy_headers(headers)}
    return [(k, v) for k, v in headers.items() if k.lower() in allowed]


def proxy_body(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or len(lengths) > 1:
        raise ValueError("Unsupported request framing")
    length = lengths[0] if lengths else "0"
    if not length.isascii() or not length.isdecimal():
        raise ValueError("Invalid content length")
    return handler.rfile.read(int(length))


def proxy_redirect(status, data, headers, path="/"):
    location = headers.get("Location")
    # Absolute destinations can expose upstream origins or another identity.
    target = urljoin(path, location).split("#", 1)[0] if location else ""
    if 300 <= status < 400 and location and not worker_allowed("GET", target):
        return 403, b'{"error":"Private redirect denied"}', {"Content-Type": "application/json"}
    return status, data, headers


class WorkerProxy:
    """Forward worker VM traffic to a hub app while denying the harness routes.

    Absolute image URLs are answered with a same-origin placeholder, as in ``WorkerAppProxy``:
    a worker VM has no route off the host, so they would render as broken boxes.
    """

    def __init__(self, upstream, host="0.0.0.0", port=0, placeholder_images=True):
        upstream = upstream.rstrip("/")
        placeholders_on = placeholder_images

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def forward(self):
                try:
                    raw = proxy_body(self)
                except ValueError:
                    self.close_connection = True
                    self.send_error(400, "Invalid request framing")
                    return
                route = self.path.split("?", 1)[0]
                if placeholders_on and route.startswith(placeholders.ROUTE):
                    # A picture the worker VM has no route to, drawn here from the token's hash.
                    if self.command in ("GET", "HEAD"):
                        status, payload, headers = placeholders.response(route[len(placeholders.ROUTE) :])
                    else:
                        status, payload, headers = 405, b"", [("Allow", "GET, HEAD")]
                    self.send_response(status)
                    for key, value in headers:
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
                    return
                body = None
                if self.command == "POST" and route == "/post":
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = None
                if not worker_allowed(self.command, self.path, body):
                    if (
                        self.command in ("GET", "HEAD")
                        and re.match(r"^/go(?:[/.?]|$)", self.path)
                        and "text/html" in self.headers.get("Accept", "")
                    ):
                        # The mock apps' own header links to the hub's app switcher; send the
                        # worker back to the app instead of a dead end that reads as a broken app.
                        query = self.path.split("?", 1)[1] if "?" in self.path else ""
                        self.send_response(302)
                        self.send_header("Location", "/" + (f"?{query}" if query else ""))
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    payload = json.dumps({"error": "Harness interface is not worker-accessible"}).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(payload)
                    return
                headers = proxy_headers(self.headers)
                request = Request(
                    upstream + self.path,
                    data=raw or None,
                    method="GET" if self.command == "HEAD" else self.command,
                    headers=headers,
                )
                try:
                    with proxy_urlopen(request, timeout=30) as response:
                        status, data, out = response.status, response.read(), response.headers
                except HTTPError as exc:
                    status, data, out = exc.code, exc.read(), exc.headers
                status, data, out = proxy_redirect(status, data, out, self.path)
                sent = proxy_response_headers(out)
                kind = placeholders.rewrite_kind(out.get("Content-Type", ""))
                if placeholders_on and status == 200 and kind and not out.get("Content-Encoding"):
                    rewritten = (placeholders.rewrite_html if kind == "html" else placeholders.rewrite_text)(
                        data
                    )
                    if rewritten != data:
                        data = rewritten
                        sent = [(k, v) for k, v in sent if k.lower() not in {"etag", "last-modified"}]
                self.send_response(status)
                for key, value in sent:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = forward

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def record_id_sets(state):
    """Record-id set per top-level key, for collections that carry ids; the wipe fingerprint."""
    from .hub_identity import _record_ids  # local: hub_identity imports this module

    return {key: ids for key, value in (state or {}).items() if (ids := _record_ids(value, key))}


def demo_replacement(state):
    """The seed with every identified record swapped for an unrelated one, as a demo save looks.

    The swapped field is whichever one ``_record_ids`` reads for that collection, not a literal
    ``id``: writing ``id`` beside an untouched ``videoId`` left every seeded id still present, so
    for any app keyed on its own singular this probe was not a replacement and the check it feeds
    could not have failed.
    """
    from .hub_identity import id_field  # local: hub_identity imports this module

    demo = deepcopy(state)
    for key in record_id_sets(state):
        value = demo[key]
        if isinstance(value, dict):
            # A map collection's keys are its ids; re-keying is the replacement.
            demo[key] = {
                f"demo-{i}": {**record, **({"id": f"demo-{i}"} if "id" in record else {})}
                for i, record in enumerate(value.values())
            }
        else:
            field = id_field(key, value)
            demo[key] = [
                {**record, field: f"demo-{i}"} if isinstance(record, dict) else record
                for i, record in enumerate(value)
            ]
    return demo


def proxy_roundtrip(base_url, sid, state, worker_id="smoke-worker"):
    """The browser-free half of the wipe check, through a real ``WorkerAppProxy``.

    What a hydrated tab does (GET ``/state``, then save that same state) must leave every
    record-id set equal to the seed's; what an unhydrated tab does (save unrelated records
    over the seed) must be refused with 409 and leave the store untouched. Returns the two
    verdicts; ``replacement_refused`` is None when the seed has no identifiable records.
    """
    from .hub_identity import WorkerAppProxy  # local: hub_identity imports this module

    upstream = HubClient(base_url)
    with WorkerAppProxy(base_url, worker_id=worker_id, sid=sid, host="127.0.0.1") as proxy:
        worker = HubClient(f"http://127.0.0.1:{proxy.port}")
        loaded = worker.current(sid)["stored_state"]
        worker.update(sid, loaded)
        preserved = record_id_sets(upstream.current(sid)["stored_state"]) == record_id_sets(state)
        refused = None
        if record_id_sets(state):
            try:
                worker.update(sid, demo_replacement(state))
                refused = False
            except HTTPError as exc:
                refused = exc.code == 409
            refused = refused and record_id_sets(upstream.current(sid)["stored_state"]) == record_id_sets(
                state
            )
    return {"record_ids_preserved": preserved, "replacement_refused": refused}


def smoke(app, source, work, state, sid="smoke", log_dir=None, schema_text=None, expect_text=None):
    """Prove the seed contract on a real hub app: seed, inspect, update, deny, reset.

    Returns a report; raises on the first failed check. This is app-level proof
    that the catalog entry is seedable and inspectable, not a task solve.
    """
    work = Path(work)
    log_dir = Path(log_dir) if log_dir else work
    log_dir.mkdir(parents=True, exist_ok=True)
    validate_state(app, state, schema_text)
    started = time.monotonic()
    built = build(source, work / app["id"], log=log_dir / f"{app['id']}-build.log")
    report = {
        "app_id": app["id"],
        "sid": sid,
        "built": str(built),
        "patches": json.loads((built / ".hub-build.json").read_text()).get("patches", []),
        "checks": {},
    }
    with HubProcess(built, log=log_dir / f"{app['id']}-serve.log") as server:
        client = server.client
        client.seed(sid, state)
        go = client.inspect(sid)
        report["checks"]["seed_is_initial_state"] = go["initial_state"] == state
        report["checks"]["seed_is_current_state"] = go["current_state"] == state
        report["checks"]["no_diff_after_seed"] = not go["state_diff"]
        key = probe_key(app, state)
        changed = {**state, key: {"_probe": True} if isinstance(state[key], dict) else [{"_probe": True}]}
        client.update(sid, changed)
        go = client.inspect(sid)
        report["checks"]["initial_preserved_after_update"] = go["initial_state"] == state
        report["checks"]["diff_visible_after_update"] = bool(go["state_diff"])
        with WorkerProxy(client.base_url, host="127.0.0.1") as proxy:
            worker = HubClient(f"http://127.0.0.1:{proxy.port}")
            denied = {}
            for name, call in (
                ("inspect", lambda: worker.inspect(sid)),
                ("seed", lambda: worker.seed(sid, state)),
                ("reset", lambda: worker.reset(sid)),
            ):
                try:
                    call()
                    denied[name] = False
                except HTTPError as exc:
                    denied[name] = exc.code == 403
            report["checks"]["worker_proxy_denies_harness_routes"] = all(denied.values())
            # 97 of the 98 clones acknowledge a save as {"success": true}; wandb_mock answers
            # {"status": "ok"}. Its /post is otherwise honest -- a failed fs.writeFileSync is caught
            # and returned as a 400 -- so only the spelling of success differs. Accept either key,
            # and only these keys: this check exists because two apps served nothing but HTTP 400
            # for hours while the suite stayed green at 2,314 tests, which a bare 2xx would hide.
            saved = worker.update(sid, changed)
            report["checks"]["worker_proxy_allows_app_saves"] = (
                saved.get("success") is True or saved.get("status") == "ok"
            )
            report["checks"]["worker_proxy_allows_app_state_load"] = (
                worker.current(sid)["has_custom_state"] is True
            )
            with urlopen(f"http://127.0.0.1:{proxy.port}/?sid={sid}", timeout=10) as page:
                report["checks"]["worker_proxy_serves_app"] = page.status == 200
            # Seed again so the identity-proxy round trip compares against the seed, not the probe.
            client.seed(sid, state)
            roundtrip = proxy_roundtrip(client.base_url, sid, state)
            report["checks"]["identity_proxy_preserves_seeded_record_ids"] = roundtrip["record_ids_preserved"]
            if roundtrip["replacement_refused"] is not None:
                report["checks"]["identity_proxy_refuses_demo_replacement"] = roundtrip["replacement_refused"]
            if expect_text:
                # Re-seed so the render check sees the seed, not the probe update.
                client.seed(sid, state)
                rendered = render_check(f"http://127.0.0.1:{proxy.port}/?sid={sid}", expect_text)
                report["render_check"] = "skipped_no_chrome" if rendered is None else rendered
                if rendered is not None:
                    report["checks"]["app_renders_seeded_content"] = rendered
        client.reset(sid)
        report["checks"]["reset_clears_session"] = client.current(sid)["has_custom_state"] is False
    report["seconds"] = round(time.monotonic() - started, 3)
    failed = sorted(name for name, ok in report["checks"].items() if not ok)
    report["passed"] = not failed
    if failed:
        raise ValueError(f"Hub smoke failed for {app['id']}: {failed}")
    return report
