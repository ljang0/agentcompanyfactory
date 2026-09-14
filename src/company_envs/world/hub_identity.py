"""Per-worker identity for shared hub apps, enforced at the proxy, not by patching apps.

Every worker VM of a company shares one app session (the same records), but reaches each
app through its own ``WorkerAppProxy``. That proxy:

- injects a small bootstrap into the app's HTML so the page always loads state from the
  server (hub apps otherwise prefer stale ``localStorage``) and reloads when a peer changed
  the shared state and this worker has been idle;
- rewrites the app's logged-in-user field (``currentUser``/``user``/``account``) in
  ``GET /state`` responses to this worker's own user record, so the app shows the worker
  as itself;
- restores the canonical user on ``set_current`` writes so the shared store never flips
  identity, and appends an attribution record (who wrote what top-level keys, when);
- replaces absolute image URLs the worker VM has no route to with a same-origin
  ``/__image/<token>`` placeholder it serves itself (``placeholder_images``);
- keeps the harness-only routes (``/go``, seed, reset) unreachable, as ``WorkerProxy`` does;
- refuses a save that could only come from an app that never loaded the shared store: one
  that drops top-level keys, replaces every record of a collection with unrelated ids, or
  removes most of a seeded collection (``refusal``).

Whole-state writes are rebased under a shared upstream lock. Conflicting fields use the
incoming value and are recorded as JSON-pointer paths (record lists use ids, not indices).
Locks coordinate proxies in this process; upstream writers must also use these proxies.
"""

import gzip
import json
import re
import secrets
import threading
import time
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from . import placeholder_images as placeholders
from .hub_access import access_errors, file_and_chat_view
from .hub_app import (
    proxy_body,
    proxy_headers,
    proxy_redirect,
    proxy_response_headers,
    top_level_keys,
    validate_sid,
    worker_allowed,
)
from .hub_app import proxy_urlopen as urlopen
from .hub_mail import browser_mail, canonical_mail
from .hub_resources import ROUTE as RESOURCE_ROUTE
from .world_check import record_key

_APP_LOCKS = {}
_APP_LOCKS_GUARD = threading.Lock()
_MISSING = object()
# Names a proxy refusal in the response, so a browser-fault check can tell a harness route the
# worker may not reach from an app request that genuinely failed.
REFUSED_HEADER = "X-Company-Envs-Refused"
VIEW_HEADER = "X-Company-Env-View"
MAX_VIEWS = 32


# One mutual-exclusion lock per upstream app, taken by reads as well as writes. Measured
# 2026-09-10 on a 5.4 MB state served through five worker proxies: replacing it with a
# readers-writer lock let the five reads run at once and made the total *worse*, 0.48 s to 0.64 s of
# wall time, because the per-read cost is CPU-bound JSON work inside one Python process and the GIL
# serialises it anyway. Five concurrent reads came back at 0.63 s each instead of 0.1/0.2/0.3/0.4/
# 0.48. So the lock is not the throughput wall and loosening it buys nothing: the wall is one
# process doing every app's parsing, rewriting and serving (51 proxies and 17 vite servers at 89%
# CPU in the live runs, where one guest read 6.0 MB in 2.2 s and three concurrent guests took
# 20-23 s). Spending that differently means more processes, not a finer lock.


# Collections that belong to one account inside a shared app. A worker sees only the records
# that involve them; everything else in the app stays shared. Fields are where a person shows
# up in a record; a match on email, id or name means the record is theirs to see.
ACCOUNT_SCOPES = {
    "emails": ("from", "to", "cc", "bcc"),
    "drafts": ("from",),
    "threads": ("participants", "from", "to"),
    "events": ("organizer", "attendees", "guests", "creator"),
    "dms": ("participants", "members", "userIds", "users"),
    "notifications": ("userId", "recipientId", "user"),
}


def _mentions(value, keys):
    """True when a party field (a string, a dict, or a list of either) names one of the keys."""
    if isinstance(value, str):
        return value.strip().casefold() in keys
    if isinstance(value, dict):
        return any(_mentions(value.get(k), keys) for k in ("email", "id", "userId", "name"))
    if isinstance(value, list):
        return any(_mentions(v, keys) for v in value)
    return False


def account_keys(user_record):
    keys = set()
    for k in ("email", "id", "userId", "name"):
        v = (user_record or {}).get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            keys.add(str(v).strip().casefold())
    return keys


def visible_to(state, user_record, keep=()):
    """The account's view of a shared app state: scoped collections filtered to this person.

    A record with none of the scope fields (a system notice, a shared thread with no party
    list) stays visible. A thread is visible when one of its visible messages names it.

    ``keep`` is the set of record ids this account wrote. Scoping answers "may I read my
    colleague's mail", not "may I see what I just did", and a worker who cannot read back its own
    write cannot verify it at all. Booking a visit for two other people made the record invisible
    to the person who booked it.
    """
    keys = account_keys(user_record)
    if not keys or not isinstance(state, dict):
        return state
    mine = {str(k) for k in (keep or ())}
    view = dict(state)
    visible_threads = set()
    for name, fields in ACCOUNT_SCOPES.items():
        rows = state.get(name)
        if not isinstance(rows, list):
            continue
        kept = []
        for r in rows:
            if not isinstance(r, dict):
                kept.append(r)
                continue
            # An empty party list is no party list: a shared reservation with no guests, a
            # notice with a blank recipient, stays visible to everyone.
            record_fields = ("from",) if name == "emails" and r.get("folder") == "drafts" else fields
            present = [f for f in record_fields if r.get(f) not in (None, "", [], {})]
            if name == "events" and not present and r.get("calendarId"):
                owner = next(
                    (c.get("userId") for c in state.get("calendars", []) if c.get("id") == r["calendarId"]),
                    None,
                )
                if owner and str(owner).casefold() not in keys:
                    continue
            own = mine and str(r.get("id", r.get("sys_id"))) in mine
            if own or not present or any(_mentions(r.get(f), keys) for f in present):
                kept.append(r)
                if r.get("threadId") is not None:
                    visible_threads.add(str(r["threadId"]))
        view[name] = kept
    if isinstance(state.get("threads"), list) and visible_threads:
        # Threads are containers: when messages carry a threadId, a thread is visible only
        # when one of this account's messages sits in it (or the thread itself names them).
        fields = ACCOUNT_SCOPES["threads"]
        view["threads"] = [
            t
            for t in state["threads"]
            if not isinstance(t, dict)
            or str(t.get("id")) in visible_threads
            or any(_mentions(t.get(f), keys) for f in fields if f in t)
        ]
    return file_and_chat_view(view, user_record)


def _json_equal(left, right):
    """JSON booleans are distinct from numbers, unlike Python's bool/int equality."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(v, right[k]) for k, v in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def _record_list(value):
    """Index record lists with unique scalar ids; other lists are atomic values."""
    if not isinstance(value, list):
        return None
    if not value:
        return {}
    field = record_key(value)
    if field is None:
        return None
    records = {}
    for record in value:
        if record[field] in records:
            return None
        records[record[field]] = record
    return records


def _rebase(base, incoming, current, conflicts, path=""):
    """Apply only base->incoming changes, recursively preserving unrelated shared edits."""
    if _json_equal(incoming, base):
        return current
    if _json_equal(current, base) or _json_equal(incoming, current):
        return incoming

    def child_path(key):
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    if (
        isinstance(incoming, dict)
        and isinstance(current, dict)
        and (isinstance(base, dict) or base is _MISSING)
    ):
        previous = {} if base is _MISSING else base
        merged = dict(current)
        for key in dict.fromkeys([*previous, *incoming]):
            value = _rebase(
                previous.get(key, _MISSING),
                incoming.get(key, _MISSING),
                current.get(key, _MISSING),
                conflicts,
                child_path(key),
            )
            if value is _MISSING:
                merged.pop(key, None)
            else:
                merged[key] = value
        return merged

    records = [_record_list(value) for value in ([] if base is _MISSING else base, incoming, current)]
    if all(value is not None for value in records):
        previous, proposed, shared = records
        merged = _rebase(previous, proposed, shared, conflicts, path)
        return list(merged.values())

    conflicts.append(path)
    return incoming


# Record-valued keys first: the key holds the logged-in person, and the proxy serves this worker's
# own record there. POINTER_CANDIDATES come after, and hold an *id* into the app's own people
# directory instead of a record. Measured over the 94 pinned schemas on 2026-09-10: 79 apps carry a
# record key, 4 carry only a pointer (clio, linear, monday, xiaohongshu), none carries both, 11
# carry neither and are single-account apps. Before the pointers were listed, identity_key()
# returned None for those four, hub_world built every proxy with user_record=None and no injection,
# and every worker VM of the company was signed in as the same person -- the direct cause of one
# company's blocking visual verdict ("shows Audrey instead of Godfrey Delacroix").
RECORD_CANDIDATES = (
    "currentUser",
    "current_user",
    "user",
    "account",
    "me",
    "profile",
    "session",
    "currentAgent",
    "loggedInUser",
    "viewer",
)
POINTER_CANDIDATES = ("currentUserId", "current_user_id")
IDENTITY_CANDIDATES = (*RECORD_CANDIDATES, *POINTER_CANDIDATES)

BOOTSTRAP = """<script data-company-envs="identity">
(function () {
  var worker = %(worker)s;
  var sid = %(sid)s;
  window.__companyWorker = worker;
  // A worker may have several tabs with different loaded snapshots.
  var view = Array.from(crypto.getRandomValues(new Uint8Array(16)), function (b) {
    return b.toString(16).padStart(2, "0");
  }).join("");
  var fetchPage = window.fetch.bind(window);
  window.fetch = function (input, options) {
    var url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin === location.origin && (url.pathname === "/state" || url.pathname === "/post")) {
      var headers = new Headers(options && options.headers !== undefined ? options.headers :
        input instanceof Request ? input.headers : undefined);
      headers.set("X-Company-Env-View", view);
      options = Object.assign({}, options, {headers: headers});
    }
    return fetchPage(input, options);
  };
  function withSid(href) {
    var url = new URL(href, location.href);
    url.searchParams.set("sid", sid);
    return url.toString();
  }
  try {
    // Every app reads its session id from the URL before sessionStorage. Keep it there, so a
    // cleared storage or a reload of an in-app route never leaves the app saving under no session.
    if (new URL(location.href).searchParams.get("sid") !== sid) {
      history.replaceState(history.state, "", withSid(location.href));
    }
  } catch (e) {}
  try {
    // Each new page must load its own server snapshot, including after a reload.
    // Native apps use both stores to decide whether they can skip requesting /state.
    localStorage.clear();
    sessionStorage.clear();
  } catch (e) {}
  var lastInput = Date.now();
  ["keydown", "mousedown", "input", "pointerdown", "wheel"].forEach(function (name) {
    window.addEventListener(name, function () { lastInput = Date.now(); }, true);
  });
  var seen = null;
  function poll() {
    fetch("/state?sid=" + encodeURIComponent(sid), {
      cache: "no-store", headers: {"X-Company-Env-Poll": "1"}
    }).then(function (r) { return r.json(); })
      .then(function (data) {
        var text = JSON.stringify(data.stored_state);
        if (seen === null) { seen = text; return; }
        if (text !== seen && Date.now() - lastInput > %(idle_ms)d) {
          // Navigate, not reload: the fresh load must carry this worker's sid in the URL.
          location.replace(withSid(location.href));
        }
      }).catch(function () {});
  }
  setInterval(poll, %(poll_ms)d);
  setTimeout(poll, 1500);
})();
</script>
"""


def identity_key(schema_text, override=None):
    """Which top-level state key holds the logged-in user; None for single-account apps."""
    if override is not None:
        return override or None
    keys = top_level_keys(schema_text) if schema_text else None
    if not keys:
        return None
    return next((key for key in IDENTITY_CANDIDATES if key in keys), None)


# How a person is identified inside an app's people directory. ``id``/``email`` alone is not
# enough: zhihu_mock keys every user record on ``userId`` and carries neither, so its worker could
# never be found in the ``users`` array it was seeded into and the whole company world was refused
# for hub serving. Matching is per field -- the same field must carry the same value on both
# records -- so a record that merely shares a name with somebody cannot pass for them.
IDENTITY_MARKERS = ("id", "email", "userId", "user_id", "sys_id", "username", "login")


def identity_markers(record):
    """The (field, value) pairs that identify a person record, in order of preference."""
    pairs = []
    for field in IDENTITY_MARKERS:
        value = (record or {}).get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            pairs.append((field, str(value).strip()))
    return pairs


def _records(collection):
    """The records of a state collection, whether it is a list or a mapping keyed by id."""
    if isinstance(collection, dict):
        return [v for v in collection.values() if isinstance(v, dict)]
    if isinstance(collection, list):
        return [v for v in collection if isinstance(v, dict)]
    return []


def is_pointer_identity(canonical):
    """Whether a seeded identity value names a person by id instead of holding their record."""
    return (
        isinstance(canonical, (str, int)) and not isinstance(canonical, bool) and bool(str(canonical).strip())
    )


def pointer_identity(state, key, record):
    """The id to serve at a pointer identity key, for one worker's own user record.

    ``currentUserId`` points into the app's own directory, so this worker is signed in by writing
    *its* id there -- never its record, which the app would render as an object where it expects a
    string. The field is the one the seeded id occupies in the directory record it names (``id`` in
    all four apps measured); a directory that names people only by its mapping keys falls back to
    the worker record's own first identifier.
    """
    canonical = state.get(key) if isinstance(state, dict) else None
    if not is_pointer_identity(canonical) or not isinstance(record, dict):
        return None
    wanted = str(canonical).strip()
    own = dict(identity_markers(record))
    for collection in (state or {}).values():
        for item in _records(collection):
            for field, marker in identity_markers(item):
                if marker == wanted and field in own:
                    return own[field]
    return next(iter(own.values()), None)


def _bootstrap_csp(policy, nonce):
    """Allow the injected script while retaining the upstream's other restrictions."""
    directives = [part.strip().split() for part in policy.split(";") if part.strip()]
    defaults = next((d[1:] for d in directives if d[0].lower() == "default-src"), None)
    if not any(d[0].lower() == "script-src" for d in directives) and defaults is not None:
        directives.append(["script-src", *defaults])
    for directive in directives:
        if directive[0].lower() in {"script-src", "script-src-elem"}:
            directive[:] = [v for v in directive if v != "'none'"] + [f"'nonce-{nonce}'"]
    return "; ".join(" ".join(d) for d in directives)


MASS_REMOVAL_MIN = 4  # a workbook's sheet list is small and still worth protecting
MASS_REMOVAL_SHARE = 0.5
MASS_REMOVAL_FLOOR = 3  # at least this many records must vanish; deleting two of five is editing


# A field name that claims to be an identifier, as a shape rather than a list of spellings. The
# list was ``id`` and ``sys_id``, and it was narrower than the data in 45 collections across 9
# apps: youtube keys on ``videoId``, salesforce on ``accountId``, slack on ``messageId``. Deriving
# ``{singular}Id`` from the collection name is not enough either -- ``opportunities`` gives
# ``opportunitieId`` and wechat's ``moments`` are keyed on ``postId`` -- so the name is matched by
# shape and then *chosen* from the records themselves.
LITERAL_IDS = ("id", "sys_id")
ID_NAME = re.compile(r"^(?:id|sys_id|uuid|guid|_id)$|.(?:Id|ID|_id)$")


def _id_candidates(records):
    """Fields every record carries whose values are distinct scalars: the possible primary keys.

    Distinctness is what separates a record's own key from a foreign one -- several answers share
    an ``authorId`` and no two share an ``answerId`` -- so it is read off the data, not declared.
    """
    shared = [key for key in records[0] if all(key in record for record in records)]
    out = []
    for key in shared:  # the record's own field order; these clones put their key first
        values = [record[key] for record in records]
        if not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in values):
            continue
        if len({str(v) for v in values}) != len(values):
            continue
        out.append(key)
    return out


def _stem(name):
    """A collection or id-field name reduced to its singular root, lowercased.

    ``removesuffix("s")`` is not enough on its own: it turns ``opportunities`` into
    ``opportunitie`` and ``activities`` into ``activitie``, which is how salesforce's two largest
    collections were missed by a derivation that was otherwise right.
    """
    name = re.sub(r"(?:Id|ID|_id)$", "", str(name))
    if name.endswith("ies"):
        return (name[:-3] + "y").lower()
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1].lower()
    return name.lower()


def _names_its_own_record(collection, field):
    """Does ``field`` name the thing the collection holds? ``opportunities`` / ``opportunityId``.

    Equal stems, or one a tail of the other so ``timeOffRequests`` admits ``requestId`` and
    ``callHistory`` admits ``callId``. The tail form needs four characters, because two-letter
    stems match anything: ``ads`` would otherwise claim ``leadId`` as its own key.
    """
    a, b = _stem(collection), _stem(field)
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= 4 and (a.endswith(b) or b.endswith(a))


def _id_field(collection, records):
    """The field a collection's records identify themselves by, or None.

    Preference order, narrowest claim first:

    1. a literal ``id``/``sys_id``;
    2. a field whose name is the collection's own record plus an id suffix -- a claim about the
       name, so one record is enough to read it;
    3. otherwise the first id-shaped name the records carry, which is a claim about the *data* and
       so needs at least two records to stand. Distinctness is the only thing separating a record's
       own key from a foreign one, and with a single record nothing is distinct: on one-record
       collections this rule chose ``regionId`` for aliyun's buckets, ``sheetId`` for a sheets
       named range and ``destinationId`` for a booking search -- all foreign keys, and all of them
       would have had ``replacement()`` refuse a worker for re-pointing a record at another parent.

    No app is named and no per-app spelling listed, so the next clone added needs no change here.
    """
    candidates = _id_candidates(records)
    if not candidates:
        return None
    for literal in LITERAL_IDS:
        if literal in candidates:
            return literal
    named = [key for key in candidates if ID_NAME.search(key)]
    own = next((key for key in named if _names_its_own_record(collection, key)), None)
    if own or len(records) < 2:
        return own
    return next(iter(named), None)


def id_field(collection, value):
    """The field ``_record_ids`` reads for a list-shaped collection, or None for any other shape.

    Exposed because a probe that swaps a collection's ids has to swap the same field this reads;
    ``hub_app.demo_replacement`` wrote a literal ``id`` and left ``videoId`` untouched, so for
    every app keyed on its own singular the demo save it built was not a replacement at all.
    """
    if not isinstance(value, list):
        return None
    records = [r for r in value if isinstance(r, dict)]
    if not records:
        return None
    # Unchanged for the 91 fixtures that already worked: a literal id anywhere in the collection
    # still wins outright, so this extends coverage and cannot move an app that had it.
    for literal in LITERAL_IDS:
        if any(literal in record for record in records):
            return literal
    return _id_field(collection, records)


def _record_ids(value, collection=""):
    if isinstance(value, dict):
        return (
            {str(k) for k in value} if value and all(isinstance(v, dict) for v in value.values()) else set()
        )
    if isinstance(value, list):
        field = id_field(collection, value)
        if field is None:
            return set()
        return {str(r[field]) for r in value if isinstance(r, dict) and field in r}
    return set()


def mass_removal(previous, state):
    """Collections where one save drops at least half of the records that were there."""
    wiped = []
    for key, before in (previous or {}).items():
        ids = _record_ids(before, key)
        if len(ids) < MASS_REMOVAL_MIN:
            continue
        after = _record_ids((state or {}).get(key), key)
        lost = len(ids - after)
        if lost >= MASS_REMOVAL_FLOOR and lost / len(ids) >= MASS_REMOVAL_SHARE:
            wiped.append({"collection": key, "before": len(ids), "removed": lost})
    return wiped


def replacement(previous, state):
    """Collections whose stored records all vanish in favour of records with unrelated ids.

    A tab that never loaded the shared store saves its demo data: every seeded id is gone and
    every incoming id is new. Deleting records (an empty collection) is editing, not replacing.
    """
    replaced = []
    for key, before in (previous or {}).items():
        ids = _record_ids(before, key)
        if not ids:
            continue
        after = _record_ids((state or {}).get(key), key)
        if after and not (ids & after):
            replaced.append({"collection": key, "before": len(ids), "incoming": len(after)})
    return replaced


def dropped_keys(previous, state):
    """Top-level keys of the stored state that a save would erase; apps never shed state keys."""
    return sorted(set(previous or {}) - set(state or {}))


def refusal(previous, state):
    """Why a save must not reach the store, as a plain-English JSON payload; None when it may.

    ``refused`` names the class of refusal, so a browser-fault check can tell a save the proxy
    deliberately turned away from an app request that genuinely broke.
    """
    if keys := dropped_keys(previous, state):
        return {
            "error": "this save drops top-level keys of the shared state, which only a partial or "
            "demo state does; refused",
            "refused": "dropped_keys",
            "keys": keys,
        }
    if replaced := replacement(previous, state):
        return {
            "error": "this save replaces every record of a seeded collection with unrelated "
            "records, which is an app starting from demo data rather than an edit; refused",
            "refused": "demo_state",
            "collections": replaced,
        }
    if wiped := mass_removal(previous, state):
        return {
            "error": "this save would remove most seeded records; refused",
            "refused": "mass_removal",
            "collections": wiped,
        }
    return None


class WorkerAppProxy:
    """One worker's view of one shared hub app."""

    # A proxy that never ran __init__ (a unit test building one with __new__) still reads back
    # correctly: an empty set changes nothing about scoping, and no pointer means the identity key
    # holds a record.
    authored = frozenset()
    user_pointer = None
    user_record = None

    def __init__(
        self,
        upstream,
        *,
        worker_id,
        sid,
        identity_key=None,
        user_record=None,
        user_pointer=None,
        canonical_user=None,
        attribution_log=None,
        host="0.0.0.0",
        port=0,
        poll_ms=5000,
        idle_ms=10000,
        merge_writes=True,
        placeholder_images=True,
        resources=None,
        on_write=None,
        app_id=None,
    ):
        upstream = upstream.rstrip("/")
        validate_sid(sid)
        if identity_key and not isinstance(user_record, dict):
            raise ValueError(f"{worker_id} needs a user record for identity key {identity_key!r}")
        if identity_key and is_pointer_identity(canonical_user) and not is_pointer_identity(user_pointer):
            # The app reads this key as an id into its own directory; handing it a record renders an
            # object where a string belongs, and handing it nothing leaves every worker as the seed's
            # one signed-in person.
            raise ValueError(f"{worker_id} needs an id for pointer identity key {identity_key!r}")
        self.worker_id, self.sid, self.identity_key = worker_id, sid, identity_key
        self.user_record, self.canonical_user = user_record, canonical_user
        self.user_pointer = user_pointer
        self.attribution_log = Path(attribution_log) if attribution_log else None
        self.merge_writes = merge_writes
        self.placeholder_images = placeholder_images
        self.resources = resources
        self.on_write = on_write
        self.app_id = app_id
        self.base = None
        self._view_bases = OrderedDict()
        # Records this worker has written through this proxy. A scoped collection hides records
        # the account is not a party to, which is right for reading a colleague's mail and wrong
        # for reading back your own work: a secretary who books a visit for the nurse and the
        # physician is neither organiser nor attendee, so her seven reservations came back
        # invisible to her. She spent the end of her budget reporting a persistence failure that
        # had not happened, and the run's handoff records a defect its own grade contradicts.
        self.authored = set()
        with _APP_LOCKS_GUARD:
            self._merge_lock = _APP_LOCKS.setdefault(upstream, threading.Lock())
        proxy = self
        bootstrap = (
            BOOTSTRAP
            % {
                "worker": json.dumps({"id": worker_id}).replace("<", "\\u003c"),
                "sid": json.dumps(sid),
                "poll_ms": poll_ms,
                "idle_ms": idle_ms,
            }
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, data, headers=()):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # Browser navigation cancelled the response; the server remains healthy.

            def upstream_call(self, method, path, raw=None, headers=None):
                request = Request(upstream + path, data=raw or None, method=method, headers=headers or {})
                try:
                    with urlopen(request, timeout=30) as response:
                        status, data, out = response.status, response.read(), response.headers
                except HTTPError as exc:
                    status, data, out = exc.code, exc.read(), exc.headers
                if any(
                    kind in out.get("Content-Type", "").lower()
                    for kind in ("text/html", "application/json", "javascript", "text/css")
                ):
                    try:
                        encoding = out.get("Content-Encoding", "identity").lower()
                        if encoding == "gzip":
                            data = gzip.decompress(data)
                        elif encoding == "deflate":
                            data = zlib.decompress(data)
                        elif encoding != "identity":
                            raise ValueError("Unsupported response encoding")
                    except (ValueError, OSError, zlib.error):
                        return 502, b"Cannot decode upstream response", {}
                    if "Content-Encoding" in out:
                        del out["Content-Encoding"]
                return status, data, out

            def forward(self):
                try:
                    raw = proxy_body(self)
                except ValueError:
                    self.close_connection = True
                    self.send_error(400, "Invalid request framing")
                    return
                route = self.path.split("?", 1)[0]
                view = self.headers.get(VIEW_HEADER)
                if view is not None and not re.fullmatch(r"[0-9a-f]{32}", view):
                    self.send(400, b"Invalid page view", [])
                    return
                if route.startswith(RESOURCE_ROUTE) and proxy.resources is not None:
                    if self.command not in ("GET", "HEAD"):
                        self.send(405, b"", [("Allow", "GET, HEAD")])
                        return
                    with proxy._merge_lock:
                        status, data, _ = self.upstream_call("GET", "/state?sid=" + proxy.sid)
                        state = json.loads(data).get("stored_state", {}) if status == 200 else {}
                        self.send(*proxy.resources.response(route, visible_to(state, proxy.user_record)))
                    return
                if proxy.placeholder_images and route.startswith(placeholders.ROUTE):
                    # Answered here, from a hash: a placeholder never reaches the app or the network.
                    if self.command in ("GET", "HEAD"):
                        self.send(*placeholders.response(route[len(placeholders.ROUTE) :]))
                    else:
                        self.send(405, b"", [("Allow", "GET, HEAD")])
                    return
                body = None
                rebased_action = None
                if self.command == "POST" and route == "/post":
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = None
                    if isinstance(body, dict) and body.get("action") == "set":
                        # ``set`` is the *seeding* action: it redefines the session's initial state,
                        # which is the grader's zero point, so a worker must never reach it. But 31
                        # of the 98 clones on disk -- 29 of the 90 in the catalogue -- POST one on
                        # load to claim their baseline, and refusing it logged a console 403 in every
                        # one of them, which made a guest-side "no browser faults" check report a
                        # fault for every app and so mean nothing. The content is still a worker's
                        # save, so it is taken as one: rebased onto the shared state, attributed, and
                        # refused by refusal() if it is an app resetting to its demo data. The
                        # baseline is never touched.
                        rebased_action = body["action"]
                        body = {**body, "action": "set_current"}
                if not worker_allowed(self.command, self.path, body):
                    if (
                        self.command in ("GET", "HEAD")
                        and re.match(r"^/go(?:[/.?]|$)", self.path)
                        and "text/html" in self.headers.get("Accept", "")
                    ):
                        # The mock apps' own header links to the hub's app switcher; a worker
                        # gets sent back to the app instead of a dead end that reads as a broken app.
                        query = self.path.split("?", 1)[1] if "?" in self.path else ""
                        self.send(302, b"", [("Location", "/" + (f"?{query}" if query else ""))])
                        return
                    # A refused harness route is not a defect of the app: say so in a way a fault
                    # check can tell apart from a failing app request.
                    payload = json.dumps(
                        {
                            "error": "Harness interface is not worker-accessible",
                            "refused": "harness_route",
                        }
                    ).encode()
                    self.send(
                        403,
                        payload,
                        [("Content-Type", "application/json"), (REFUSED_HEADER, "harness_route")],
                    )
                    return
                if route == "/post" and not isinstance(body.get("state"), dict):
                    self.send(
                        400, b'{"error":"State must be an object"}', [("Content-Type", "application/json")]
                    )
                    return
                parsed = urlsplit(self.path)
                query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "sid"]
                path = urlunsplit(parsed._replace(query=urlencode([*query, ("sid", proxy.sid)])))
                headers = proxy_headers(self.headers)
                headers["Accept-Encoding"] = "identity"
                method = "GET" if self.command == "HEAD" else self.command
                if (
                    body is not None
                    and body.get("action") == "set_current"
                    and isinstance(body.get("state"), dict)
                ):
                    with proxy._merge_lock:
                        if view is not None and view not in proxy._view_bases:
                            self.send(409, b"Reload this page before saving its state", [])
                            return
                        with proxy.view_base(view):
                            status, data, out = proxy.write_state(
                                body, path, headers, self.upstream_call, rebased_action=rebased_action
                            )
                        if status == 200 and proxy.identity_key:
                            payload = json.loads(data)
                            if isinstance(payload, dict) and isinstance(payload.get("state"), dict):
                                payload["state"][proxy.identity_key] = proxy.identity_value
                                data = json.dumps(payload).encode()
                elif route == "/state" and method == "GET":
                    with proxy._merge_lock, proxy.view_base(view):
                        status, data, out = self.upstream_call(method, path, raw, headers)
                        if status == 200:
                            # Polls observe peer changes without loading them into the UI.
                            data = proxy.rewrite_state(
                                data,
                                remember=self.command != "HEAD"
                                and self.headers.get("X-Company-Env-Poll") != "1",
                            )
                else:
                    status, data, out = self.upstream_call(method, path, raw, headers)
                status, data, out = proxy_redirect(status, data, out, self.path)
                content_type = out.get("Content-Type", "")
                passthrough = proxy_response_headers(out)
                if "text/html" in content_type and status == 200:
                    try:
                        message = Message()
                        message["Content-Type"] = content_type
                        charset = message.get_content_charset() or "utf-8"
                        data = data.decode(charset).encode("utf-8")
                    except (ValueError, LookupError):
                        self.send(502, b"Cannot decode upstream HTML")
                        return
                    if proxy.placeholder_images:
                        data = placeholders.rewrite_html(data)
                    nonce = secrets.token_hex(16)
                    script = bootstrap.replace(b"<script ", f'<script nonce="{nonce}" '.encode(), 1)
                    data = proxy.inject(data, script)
                    passthrough = [
                        (k, _bootstrap_csp(v, nonce) if k.lower() == "content-security-policy" else v)
                        for k, v in passthrough
                        if k.lower() not in {"content-encoding", "content-type", "etag", "last-modified"}
                    ]
                    passthrough.append(("Content-Type", "text/html; charset=utf-8"))
                elif (
                    proxy.placeholder_images
                    and status == 200
                    and not out.get("Content-Encoding")
                    and placeholders.rewrite_kind(content_type) == "text"
                ):
                    # The clones' avatars and logos live in the bundles, not only in the markup.
                    rewritten = placeholders.rewrite_text(data)
                    if rewritten != data:
                        # A revalidation of the old bytes must not hand the app the old bundle back.
                        data = rewritten
                        passthrough = [
                            (k, v) for k, v in passthrough if k.lower() not in {"etag", "last-modified"}
                        ]
                if route in {"/state", "/post"} or "text/html" in content_type:
                    passthrough = [
                        (k, v)
                        for k, v in passthrough
                        if k.lower() not in {"cache-control", "etag", "last-modified"}
                    ]
                    passthrough.append(("Cache-Control", "no-store"))
                if route in {"/state", "/post"} and status == 200:
                    passthrough = [(k, v) for k, v in passthrough if k.lower() != "content-type"]
                    passthrough.append(("Content-Type", "application/json; charset=utf-8"))
                self.send(status, data, passthrough)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = forward

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def identity_value(self):
        """What this worker's identity key holds: its own record, or its id for a pointer key."""
        return self.user_record if self.user_pointer is None else self.user_pointer

    @contextmanager
    def view_base(self, view):
        """Select a page's loaded snapshot while the upstream merge lock is held."""
        if view is None:
            yield
            return
        default = self.base
        self.base = self._view_bases.pop(view, None)
        try:
            yield
        finally:
            if self.base is not None:
                self._view_bases[view] = self.base
                while len(self._view_bases) > MAX_VIEWS:
                    self._view_bases.popitem(last=False)
            self.base = default

    @staticmethod
    def inject(html, bootstrap):
        head = re.search(rb"<head\b[^>]*>", html, re.IGNORECASE)
        if head:
            index = head.end()
        else:
            opening = re.search(rb"<html\b[^>]*>|<!doctype\b[^>]*>", html, re.IGNORECASE)
            # Prefer the html element when a doctype precedes it.
            opening = re.search(rb"<html\b[^>]*>", html, re.IGNORECASE) or opening
            index = opening.end() if opening else 0
            bootstrap = b"<head>" + bootstrap + b"</head>"
        return html[:index] + b"\n" + bootstrap + html[index:]

    def rewrite_state(self, data, *, remember=True):
        try:
            payload = json.loads(data)
        except ValueError:
            return data
        stored = payload.get("stored_state") if isinstance(payload, dict) else None
        if remember:
            self.base = deepcopy(stored) if isinstance(stored, dict) else None
        if isinstance(stored, dict):
            stored = visible_to(stored, self.user_record, keep=self.authored)
            if self.identity_key:
                stored[self.identity_key] = self.identity_value
            if remember:
                self.base = deepcopy(stored)
            if self.placeholder_images:
                # What the app is handed, not what is stored: a picture field the browser cannot
                # draw is replaced here, after ``self.base`` has kept the shape a later save is
                # compared against. Apps merge their own defaults underneath the state they are
                # given, so a person with no picture field at all renders an empty avatar.
                stored = placeholders.reset_transient(placeholders.fill_image_fields(stored))
            if getattr(self, "app_id", None) == "gmail_mock":
                stored = browser_mail(stored)
            if getattr(self, "app_id", None) == "google_calendar_mock":
                uid = (self.user_record or {}).get("id")
                stored["calendars"] = sorted(
                    stored.get("calendars", []), key=lambda c: c.get("userId") != uid
                )
            if getattr(self, "resources", None) is not None:
                stored = self.resources.rewrite(stored)
            payload["stored_state"] = stored
            return json.dumps(payload).encode()
        return data

    def write_state(self, body, path, headers, upstream_call, *, rebased_action=None):
        """Read, rebase, persist and attribute under the caller's upstream lock.

        ``rebased_action`` names the action the worker actually sent when it was not
        ``set_current`` (an app's load-time ``set``), so the attribution log says so.
        """
        if getattr(self, "resources", None) is not None:
            body = {**body, "state": self.resources.rewrite(body["state"], reverse=True)}
        if self.placeholder_images and isinstance(body.get("state"), dict):
            # The app was served pictures it could draw; the store keeps what the world says.
            body = {**body, "state": placeholders.strip_placeholders(body["state"], self.base)}
        previous = {}
        if self.merge_writes or self.attribution_log is not None:
            # Match the actual write session, including the upstream's default sid.
            query = "?" + path.split("?", 1)[1] if "?" in path else ""
            status, data, out = upstream_call("GET", "/state" + query)
            if status != 200 and self.merge_writes:
                return status, data, out
            try:
                payload = json.loads(data)
                previous = payload["stored_state"]
                if previous is None:
                    previous = {}
                if not isinstance(previous, dict):
                    raise TypeError("Shared state must be an object")
            except (ValueError, KeyError, TypeError):
                if self.merge_writes:
                    return (
                        502,
                        b'{"error": "Cannot read shared state for merge"}',
                        {"Content-Type": "application/json"},
                    )
                previous = {}

        incoming = deepcopy(body["state"])
        if getattr(self, "app_id", None) == "gmail_mock":
            incoming = canonical_mail(incoming)
        if self.user_record:
            errors = access_errors(previous, incoming, self.base, self.user_record)
            if errors:
                return (
                    403,
                    json.dumps({"error": "Record access denied", "details": errors}).encode(),
                    {"Content-Type": "application/json"},
                )
        if self.identity_key:
            incoming[self.identity_key] = self.canonical_user
        conflicts = []
        state = (
            _rebase(self.base if self.base is not None else previous, incoming, previous, conflicts)
            if self.merge_writes
            else incoming
        )
        if self.identity_key:
            state[self.identity_key] = self.canonical_user
        refused = refusal(previous, state)
        if refused:
            # A single save that sheds keys, swaps every record or drops most of a seeded
            # collection is an app resetting to its demo data (or a stale tab), never a worker's
            # edit. Refuse it so the world survives.
            return 409, json.dumps(refused).encode(), {"Content-Type": "application/json"}
        raw = json.dumps({**body, "state": state}).encode()
        headers = {k: v for k, v in headers.items() if k.lower() != "content-length"}
        headers["Content-Length"] = str(len(raw))
        response = upstream_call("POST", path, raw, headers)
        if 200 <= response[0] < 300:
            if getattr(self, "on_write", None) is not None:
                self.on_write(self.worker_id, previous, state)
            # The UI still holds incoming: merged peer edits were never served to it.
            self.base = deepcopy(incoming)
            self.remember_authored(state, previous)
            self.record_write(state, previous, conflicts, rebased_action=rebased_action)
        return response

    def remember_authored(self, state, previous):
        """Ids this save added, so the account can read back its own work.

        A scoped collection hides records the account is not a party to. That is right for a
        colleague's mail and wrong for a record this worker just created for somebody else.
        """
        for name in ACCOUNT_SCOPES:
            added = _record_ids(state.get(name), name) - _record_ids(previous.get(name), name)
            self.authored |= added

    def record_write(self, state, previous, conflicts, *, rebased_action=None):
        """Attribute successful writes and conflicting JSON-pointer field paths."""
        if self.attribution_log is None:
            return
        changed = sorted(
            key
            for key in set(previous) | set(state)
            if key != self.identity_key
            and not _json_equal(previous.get(key, _MISSING), state.get(key, _MISSING))
        )
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "worker_id": self.worker_id,
            "sid": self.sid,
            "changed_keys": changed,
            "conflicts": conflicts,
        }
        if rebased_action:
            # The app asked to seed; the proxy wrote the content as a current-state save instead.
            entry["rebased_action"] = rebased_action
        self.attribution_log.parent.mkdir(parents=True, exist_ok=True)
        with self.attribution_log.open("a") as stream:
            stream.write(json.dumps(entry) + "\n")

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
