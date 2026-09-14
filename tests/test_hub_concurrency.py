"""Shared hub writes through real proxies and a protocol double; no browser or VM."""

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from test_hub_app import StubHub

from company_envs.world import hub_identity
from company_envs.world.hub_app import HubClient
from company_envs.world.hub_identity import WorkerAppProxy, _rebase

SID = "shared"
USERS = [
    {"id": 1, "name": "Dana Ortiz", "email": "dana.ortiz@acme.test"},
    {"id": 2, "name": "Luis Prado", "email": "luis.prado@acme.test"},
]
STATE = {
    "currentUser": USERS[0],
    "users": USERS,
    "tickets": [{"id": 7, "status": "open", "subject": "Delivery"}],
    "comments": {"7": [{"id": 1, "body": "Original"}]},
}


@contextmanager
def workers(tmp_path, *, merge_writes=True, identity=True):
    with ExitStack() as stack:
        stub = StubHub()
        stack.callback(stub.close)
        shared = HubClient(stub.url)
        shared.seed(SID, STATE)
        log = tmp_path / "runtime" / "attribution" / "demo_mock.jsonl"
        proxies = [
            stack.enter_context(
                WorkerAppProxy(
                    stub.url + ("/" if index else ""),
                    worker_id=f"w{index + 1}",
                    sid=SID,
                    identity_key="currentUser" if identity else None,
                    user_record=user if identity else None,
                    canonical_user=USERS[0] if identity else None,
                    attribution_log=log,
                    host="127.0.0.1",
                    merge_writes=merge_writes,
                )
            )
            for index, user in enumerate(USERS)
        ]
        clients = [HubClient(f"http://127.0.0.1:{proxy.port}") for proxy in proxies]
        states = [client.current(SID)["stored_state"] for client in clients]
        yield shared, clients, states, log, proxies


@pytest.mark.parametrize("identity", [True, False])
def test_stale_additions_and_subsequent_writes_preserve_peer_records(tmp_path, identity):
    with workers(tmp_path, identity=identity) as (shared, clients, states, log, proxies):
        for client, state, ticket_id in zip(clients, states, (8, 9), strict=True):
            state["tickets"].append({"id": ticket_id, "status": "open"})
            assert client.update(SID, state)["success"]
        assert {ticket["id"] for ticket in shared.current(SID)["stored_state"]["tickets"]} == {7, 8, 9}
        # B's browser has never seen ticket 8, including after its first save.
        states[1]["tickets"][1]["status"] = "closed"
        clients[1].update(SID, states[1])
        tickets = {ticket["id"]: ticket for ticket in shared.current(SID)["stored_state"]["tickets"]}
        assert set(tickets) == {7, 8, 9}
        assert tickets[9]["status"] == "closed"
        assert proxies[1].base == {**states[1], "currentUser": USERS[0]}
        assert all(not json.loads(line)["conflicts"] for line in log.read_text().splitlines())


def test_ticket_status_and_comment_edits_survive(tmp_path):
    with workers(tmp_path) as (shared, clients, states, log, _):
        states[0]["tickets"][0]["status"] = "pending"
        states[1]["comments"]["7"].append({"id": 2, "body": "Worker B replied"})
        clients[0].update(SID, states[0])
        clients[1].update(SID, states[1])
        current = shared.current(SID)["stored_state"]
        assert current["tickets"][0]["status"] == "pending"
        assert current["comments"]["7"] == states[1]["comments"]["7"]
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert [entry["changed_keys"] for entry in entries] == [["tickets"], ["comments"]]
        assert all(entry["conflicts"] == [] for entry in entries)


def test_conflicting_field_uses_incoming_and_is_attributed(tmp_path):
    with workers(tmp_path) as (shared, clients, states, log, _):
        states[0]["tickets"][0].update(status="pending", subject="Updated by A")
        states[1]["tickets"][0]["status"] = "closed"
        clients[0].update(SID, states[0])
        clients[1].update(SID, states[1])
        assert shared.current(SID)["stored_state"]["tickets"] == [
            {"id": 7, "status": "closed", "subject": "Updated by A"}
        ]
        entry = json.loads(log.read_text().splitlines()[-1])
        assert entry["worker_id"] == "w2"
        assert entry["sid"] == SID
        assert entry["changed_keys"] == ["tickets"]
        assert entry["conflicts"] == ["/tickets/7/status"]


def test_merge_disabled_reproduces_last_writer_wins(tmp_path):
    with workers(tmp_path, merge_writes=False) as (shared, clients, states, log, _):
        states[0]["tickets"].append({"id": 8})
        states[1]["tickets"].append({"id": 9})
        clients[0].update(SID, states[0])
        clients[1].update(SID, states[1])
        current = shared.current(SID)["stored_state"]
        assert {ticket["id"] for ticket in current["tickets"]} == {7, 9}
        assert current["currentUser"] == USERS[0]
        assert json.loads(log.read_text().splitlines()[-1])["conflicts"] == []


def test_read_refreshes_base_identity_bootstrap_and_denials_are_preserved(tmp_path):
    with workers(tmp_path) as (shared, clients, states, _, proxies):
        assert [state["currentUser"] for state in states] == USERS
        states[0]["tickets"][0]["status"] = "pending"
        clients[0].update(SID, states[0])
        refreshed = clients[1].current(SID)["stored_state"]
        assert refreshed["currentUser"] == USERS[1]
        # An intentional change back to the seed value must use the refreshed base.
        refreshed["tickets"][0]["status"] = "open"
        clients[1].update(SID, refreshed)
        assert shared.current(SID)["stored_state"] == STATE
        for client, proxy in zip(clients, proxies, strict=True):
            for action in (
                lambda client=client: client.inspect(SID),
                lambda client=client: client.reset(SID),
            ):
                with pytest.raises(HTTPError) as denied:
                    action()
                assert denied.value.code == 403
            # A worker's seed is admitted as a current-state save and can never reach the baseline,
            # because 31 clones post one on load and a blanket 403 made every app log a console
            # fault. The privilege is unchanged: the same content was always writable through
            # set_current, and initial_state still is not.
            before = shared.inspect(SID)["initial_state"]
            client.seed(SID, STATE)
            assert shared.inspect(SID)["initial_state"] == before
            with urlopen(f"http://127.0.0.1:{proxy.port}/?sid={SID}", timeout=5) as response:
                page = response.read()
            assert b'data-company-envs="identity"' in page
            assert f'"id": "{proxy.worker_id}"'.encode() in page
            assert b"localStorage.clear()" in page
            # The idle reload navigates to a URL carrying this worker's sid; a bare reload of an
            # in-app route left the app without a session id and saving its demo state.
            assert b"location.reload()" not in page
            assert b"location.replace(withSid(location.href))" in page
            assert b'url.searchParams.set("sid", sid)' in page
            assert b'history.replaceState(history.state, "", withSid(location.href))' in page


def test_background_poll_does_not_adopt_state_that_ui_has_not_loaded(tmp_path):
    with workers(tmp_path) as (shared, clients, states, _, proxies):
        states[0]["tickets"].append({"id": 8})
        clients[0].update(SID, states[0])
        request = Request(
            f"http://127.0.0.1:{proxies[1].port}/state?sid={SID}",
            headers={"X-Company-Env-Poll": "1"},
        )
        with urlopen(request, timeout=5) as response:
            observed = json.load(response)["stored_state"]
        assert observed["tickets"] == states[0]["tickets"]
        assert observed["currentUser"] == USERS[1]
        states[1]["tickets"].append({"id": 9})
        clients[1].update(SID, states[1])
        assert {ticket["id"] for ticket in shared.current(SID)["stored_state"]["tickets"]} == {7, 8, 9}


def page_state(proxy, view, state=None, *, poll=False):
    headers = {"X-Company-Env-View": view}
    if poll:
        headers["X-Company-Env-Poll"] = "1"
    route = "state" if state is None else "post"
    body = None if state is None else json.dumps({"action": "set_current", "state": state}).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"http://127.0.0.1:{proxy.port}/{route}?sid={SID}", data=body, headers=headers)
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_a_second_tab_read_cannot_turn_a_stale_save_into_a_peer_record_deletion(tmp_path):
    with workers(tmp_path) as (shared, clients, states, _, proxies):
        old_tab, new_tab = "a" * 32, "b" * 32
        stale = page_state(proxies[1], old_tab)["stored_state"]
        states[0]["tickets"].append({"id": 8, "status": "open"})
        clients[0].update(SID, states[0])
        fresh = page_state(proxies[1], new_tab)["stored_state"]
        assert {t["id"] for t in fresh["tickets"]} == {7, 8}
        page_state(proxies[1], old_tab, poll=True)
        stale["tickets"][0]["subject"] = "Updated in the original tab"
        page_state(proxies[1], old_tab, stale)
        current = shared.current(SID)["stored_state"]
        assert {t["id"] for t in current["tickets"]} == {7, 8}
        assert current["tickets"][0]["subject"] == "Updated in the original tab"
        # Deliberately deleting a record that this page loaded is still possible.
        fresh["tickets"] = [t for t in fresh["tickets"] if t["id"] != 8]
        page_state(proxies[1], new_tab, fresh)
        assert shared.current(SID)["stored_state"]["tickets"] == [current["tickets"][0]]


def test_unknown_or_evicted_page_must_load_state_before_saving(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_identity, "MAX_VIEWS", 2)
    with workers(tmp_path) as (shared, _, states, _, proxies):
        for view in ("a" * 32, "b" * 32, "c" * 32):
            page_state(proxies[0], view)
        assert len(proxies[0]._view_bases) == 2
        for view in ("a" * 32, "d" * 32):
            # A background poll does not establish that the UI loaded this state.
            page_state(proxies[0], view, poll=True)
            with pytest.raises(HTTPError) as error:
                page_state(proxies[0], view, states[0])
            assert error.value.code == 409
        with pytest.raises(HTTPError) as error:
            page_state(proxies[0], "not-a-page-id")
        assert error.value.code == 400
        assert shared.current(SID)["stored_state"] == STATE


def test_simultaneous_writes_lock_the_entire_read_merge_write(tmp_path, monkeypatch):
    with workers(tmp_path) as (shared, clients, states, _, proxies):
        lock = proxies[0]._merge_lock
        assert proxies[1]._merge_lock is lock
        first_post = threading.Event()
        second_attempt = threading.Event()
        first_saved = threading.Event()
        original_urlopen = hub_identity.urlopen
        reads = []

        class ObservedLock:
            def __enter__(self):
                second_attempt.set()
                lock.acquire()

            def __exit__(self, *exc):
                lock.release()

        def gated_urlopen(request, **kwargs):
            if request.get_method() == "GET":
                reads.append(first_saved.is_set())
            elif not first_post.is_set():
                first_post.set()
                assert second_attempt.wait(5)
                response = original_urlopen(request, **kwargs)
                first_saved.set()
                return response
            return original_urlopen(request, **kwargs)

        proxies[1]._merge_lock = ObservedLock()
        states[0]["tickets"].append({"id": 8})
        states[1]["tickets"].append({"id": 9})
        with monkeypatch.context() as patch:
            patch.setattr(hub_identity, "urlopen", gated_urlopen)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(clients[0].update, SID, states[0])
                assert first_post.wait(5)
                second = pool.submit(clients[1].update, SID, states[1])
                assert first.result(timeout=5)["success"]
                assert second.result(timeout=5)["success"]
        assert reads == [False, True]
        assert {ticket["id"] for ticket in shared.current(SID)["stored_state"]["tickets"]} == {7, 8, 9}


@pytest.mark.parametrize("as_list", [False, True])
def test_record_add_remove_and_field_changes_in_both_collection_shapes(as_list):
    def collection(records):
        return list(records.values()) if as_list else records

    base = {"rows": collection({"1": {"id": 1, "a": 0, "b": 0}, "2": {"id": 2}})}
    incoming = {"rows": collection({"1": {"id": 1, "a": 1, "b": 0}, "3": {"id": 3}})}
    shared = {"rows": collection({"1": {"id": 1, "a": 0, "b": 1}, "2": {"id": 2}, "4": {"id": 4}})}
    expected = {"rows": collection({"1": {"id": 1, "a": 1, "b": 1}, "4": {"id": 4}, "3": {"id": 3}})}
    originals = deepcopy((base, incoming, shared))
    conflicts = []
    assert _rebase(base, incoming, shared, conflicts) == expected
    assert conflicts == []
    assert (base, incoming, shared) == originals


@pytest.mark.parametrize(
    "base,incoming,shared,expected,paths",
    [
        ({"x": 0}, {"x": 1}, {"x": 2}, {"x": 1}, ["/x"]),
        ({"x": 0}, {"x": 1}, {"x": 1}, {"x": 1}, []),
        ({"x": None}, {}, {"x": 1}, {}, ["/x"]),
        ({}, {"x": None}, {"peer": 1}, {"peer": 1, "x": None}, []),
        ({"row": {"a": 0}}, {}, {"row": {"a": 1}}, {}, ["/row"]),
        ({"row": {"a": 0, "b": 1}}, {"row": {"a": 1, "b": 1}}, {}, {"row": {"a": 1, "b": 1}}, ["/row"]),
        ({"row": {"a": 0}}, {"row": {"a": 0}}, {}, {}, []),
        ({"row": {"a": 0, "b": 0}}, {"row": {"b": 0}}, {"row": {"a": 0, "b": 1}}, {"row": {"b": 1}}, []),
        ({"x": [1]}, {"x": [1, 2]}, {"x": [1, 3]}, {"x": [1, 2]}, ["/x"]),
        ({"a/b~c": 0}, {"a/b~c": 1}, {"a/b~c": 2}, {"a/b~c": 1}, ["/a~1b~0c"]),
        (
            {"comments": {}},
            {"comments": {"7": [{"id": 1}]}},
            {"comments": {"7": [{"id": 2}]}},
            {"comments": {"7": [{"id": 2}, {"id": 1}]}},
            [],
        ),
    ],
)
def test_structural_merge_edges(base, incoming, shared, expected, paths):
    conflicts = []
    assert _rebase(base, incoming, shared, conflicts) == expected
    assert conflicts == paths


@pytest.mark.parametrize("failure", ["read_status", "read_json", "write"])
def test_failed_upstream_does_not_advance_base_or_attribute(tmp_path, failure):
    proxy = WorkerAppProxy("http://unused", worker_id="w", sid=SID, attribution_log=tmp_path / "log")
    # Exercise the write transaction directly with a double; no server thread needed.
    try:
        proxy.base = deepcopy(STATE)
        incoming = {**STATE, "tickets": [*STATE["tickets"], {"id": 8}]}
        calls = []

        def upstream(method, path, raw=None, headers=None):
            calls.append(method)
            if method == "GET":
                if failure == "read_status":
                    return 503, b"unavailable", {}
                if failure == "read_json":
                    return 200, b"{}", {}
                return 200, json.dumps({"stored_state": STATE}).encode(), {}
            return 500, b"failed", {}

        status, _, _ = proxy.write_state(
            {"action": "set_current", "state": incoming}, f"/post?sid={SID}", {}, upstream
        )
        assert status >= 500
        assert proxy.base == STATE
        assert not proxy.attribution_log.exists()
        assert calls == (["GET", "POST"] if failure == "write" else ["GET"])
    finally:
        proxy.server.server_close()


@pytest.mark.parametrize("query", ["?sid=other", "?sid=shared&sid=other", "?%73id=other", ""])
def test_worker_proxy_pins_session_on_reads_and_writes(tmp_path, query):
    with workers(tmp_path) as (shared, clients, _, log, _):
        shared.seed("other", {"private": True})
        origin = clients[1].base_url
        with urlopen(origin + "/state" + query, timeout=5) as response:
            served = json.load(response)["stored_state"]["currentUser"]
            assert {k: v for k, v in served.items() if k != "avatar"} == USERS[1]
        request = Request(
            origin + "/post" + query,
            data=json.dumps({"action": "set_current", "state": {**STATE, "tickets": []}}).encode(),
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
        assert shared.current("other")["stored_state"] == {"private": True}
        assert shared.current(SID)["stored_state"]["tickets"] == []
        assert json.loads(log.read_text().splitlines()[-1])["sid"] == SID


def test_bootstrap_preserves_doctype_and_handles_head_attributes():
    bootstrap = b"<script>bootstrap</script>"
    for page in (
        b"<!doctype html><html><body>app</body></html>",
        b'<!doctype html><HTML><HEAD lang="en"></HEAD><body>app</body></HTML>',
    ):
        injected = WorkerAppProxy.inject(page, bootstrap)
        assert injected.startswith(b"<!doctype html>")
        assert injected.lower().index(b"<html>") < injected.index(bootstrap) < injected.index(b"<body>")


def test_each_document_clears_stale_cache_and_fetches_with_its_own_view(tmp_path):
    script = hub_identity.BOOTSTRAP % {"worker": "{}", "sid": '"shared"', "idle_ms": 10000, "poll_ms": 5000}
    script = script.split(">", 1)[1].split("</script>", 1)[0]
    (tmp_path / "bootstrap.js").write_text(script)
    result = subprocess.run(
        [
            "node",
            "-e",
            r"""
const vm = require('node:vm'), fs = require('node:fs');
const script = fs.readFileSync(process.argv[1], 'utf8');
function document() {
  const calls = [], storage = {cleared: 0, clear() { this.cleared++; }};
  const context = {
    crypto: require('node:crypto').webcrypto, URL, Request, Headers,
    location: {href: 'http://app/?sid=shared', origin: 'http://app'},
    localStorage: storage, sessionStorage: {getItem: () => '1', clear() {}},
    setInterval() {}, setTimeout() {},
    window: {addEventListener() {}, fetch(input, options) {
      calls.push(Object.fromEntries(new Headers(options?.headers)));
      return Promise.resolve({});
    }}
  };
  vm.runInNewContext(script, context);
  context.window.fetch('/state?sid=shared', {headers: {'X-Company-Env-Poll': '1'}});
  context.window.fetch(new Request('http://app/post', {headers: {'X-App': 'kept'}}));
  context.window.fetch('https://external.test/state');
  context.window.fetch('/other');
  return {cleared: storage.cleared, calls};
}
process.stdout.write(JSON.stringify([document(), document()]));
""",
            str(tmp_path / "bootstrap.js"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pages = json.loads(result.stdout)
    assert pages[0]["calls"][0]["x-company-env-view"] != pages[1]["calls"][0]["x-company-env-view"]
    for page in pages:
        assert page["cleared"] == 1
        poll, post, external, other = page["calls"]
        assert poll["x-company-env-view"] == post["x-company-env-view"]
        assert poll["x-company-env-poll"] == "1" and post["x-app"] == "kept"
        assert external == other == {}


def test_proxy_rejects_invalid_session_id():
    proxy = WorkerAppProxy(
        "http://unused", worker_id="</script><script>alert(1)</script>", sid=SID, host="127.0.0.1"
    )
    try:
        # Inspection of the served HTML is covered by the HTTP fixture below.
        with pytest.raises(ValueError, match="Session"):
            WorkerAppProxy("http://unused", worker_id="w", sid="../other")
    finally:
        proxy.server.server_close()


@contextmanager
def response_proxy(
    tmp_path,
    *,
    body=b"<html><body>app</body></html>",
    content_type="text/html",
    extra=(),
    status=200,
    generic=False,
    **proxy_kwargs,
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from company_envs.world.hub_app import WorkerProxy

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for key, value in extra:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

        do_POST = do_GET

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    upstream = f"http://127.0.0.1:{server.server_port}"
    factory = (
        WorkerProxy
        if generic
        else lambda url, **kwargs: WorkerAppProxy(
            url,
            worker_id="w2",
            sid=SID,
            identity_key="currentUser",
            user_record=USERS[1],
            canonical_user=USERS[0],
            **kwargs,
        )
    )
    try:
        with factory(upstream, host="127.0.0.1", **proxy_kwargs) as proxy:
            yield f"http://127.0.0.1:{proxy.port}", calls
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("generic", [True, False])
def test_proxy_never_follows_upstream_redirect_to_harness(tmp_path, generic):
    with response_proxy(
        tmp_path, body=b"", status=302, extra=[("Location", "/go?sid=shared")], generic=generic
    ) as (origin, calls):
        with pytest.raises(HTTPError) as denied:
            urlopen(origin + "/redirect", timeout=5)
        assert denied.value.code == 403
        assert len(calls) == 1


@pytest.mark.parametrize("generic", [True, False])
def test_head_has_no_body_and_matches_get_length(tmp_path, generic):
    import http.client

    with response_proxy(tmp_path, generic=generic) as (origin, _):
        conn = http.client.HTTPConnection(origin.removeprefix("http://"), timeout=5)
        try:
            conn.request("HEAD", "/")
            response = conn.getresponse()
            length = int(response.getheader("Content-Length"))
            assert response.read() == b""
            conn.request("GET", "/")
            response = conn.getresponse()
            assert len(response.read()) == length
        finally:
            conn.close()


def test_proxy_preserves_csp_and_authorizes_only_bootstrap(tmp_path):
    with response_proxy(
        tmp_path,
        extra=[("Content-Security-Policy", "default-src 'self'; script-src 'self'; frame-ancestors 'none'")],
    ) as (origin, _):
        with urlopen(origin, timeout=5) as response:
            policy = response.headers.get("Content-Security-Policy", "")
            page = response.read()
        assert "frame-ancestors 'none'" in policy
        assert "'unsafe-inline'" not in policy
        assert "'nonce-" in policy
        nonce = policy.split("'nonce-", 1)[1].split("'", 1)[0]
        assert f'nonce="{nonce}"'.encode() in page


@pytest.mark.parametrize("encoding", ["gzip", "utf-16"])
def test_encoded_html_gets_valid_bootstrap(tmp_path, encoding):
    import gzip

    text = "<!doctype html><html><head></head><body>café</body></html>"
    body = gzip.compress(text.encode()) if encoding == "gzip" else text.encode("utf-16")
    extra = [("Content-Encoding", "gzip")] if encoding == "gzip" else []
    content_type = "text/html; charset=" + ("utf-8" if encoding == "gzip" else "utf-16")
    with response_proxy(tmp_path, body=body, content_type=content_type, extra=extra) as (origin, _):
        with urlopen(origin, timeout=5) as response:
            assert response.headers.get("Content-Encoding") is None
            page = response.read().decode("utf-8")
            assert "charset=utf-8" in response.headers["Content-Type"]
        assert page.startswith("<!doctype html>")
        assert "café" in page and 'data-company-envs="identity"' in page


def test_save_response_does_not_reveal_canonical_identity(tmp_path):
    body = json.dumps({"success": True, "stored_state": STATE, "state": STATE}).encode()
    with response_proxy(tmp_path, body=body, content_type="application/json") as (origin, _):
        request = Request(
            origin + "/post", data=json.dumps({"action": "set_current", "state": STATE}).encode()
        )
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["state"]["currentUser"] == USERS[1]


@pytest.mark.parametrize("as_list", [False, True])
def test_rebase_distinguishes_json_booleans_from_numbers(as_list):
    def shape(value):
        return [{"id": 7, "flag": value}] if as_list else {"7": {"flag": value}}

    conflicts = []
    assert _rebase(shape(False), shape(0), shape(2), conflicts) == shape(0)
    assert conflicts == ["/7/flag"]


def test_null_canonical_identity_is_restored(tmp_path):
    with workers(tmp_path) as (shared, clients, states, _, proxies):
        proxies[1].canonical_user = None
        shared.seed(SID, {**STATE, "currentUser": None})
        states[1] = clients[1].current(SID)["stored_state"]
        clients[1].update(SID, states[1])
        assert shared.current(SID)["stored_state"]["currentUser"] is None


@pytest.mark.parametrize("state", [[], None, "bad"])
def test_worker_cannot_save_non_object_state(tmp_path, state):
    with workers(tmp_path) as (shared, clients, _, _, _):
        with pytest.raises(HTTPError) as denied:
            clients[1].update(SID, state)
        assert denied.value.code == 400
        assert shared.current(SID)["stored_state"] == STATE


@pytest.mark.parametrize("generic", [True, False])
def test_chunked_binary_responses_are_decoded_and_reframed(tmp_path, generic):
    # The response double sends the wire chunks; urllib removes their framing.
    chunks = b"4\r\n\x00\xffab\r\n0\r\n\r\n"
    with (
        response_proxy(
            tmp_path,
            body=chunks,
            content_type="application/octet-stream",
            extra=[("Transfer-Encoding", "chunked")],
            generic=generic,
        ) as (origin, _),
        urlopen(origin + "/asset", timeout=5) as response,
    ):
        assert response.headers.get("Transfer-Encoding") is None
        assert response.headers.get("Content-Length") == "4"
        assert response.read() == b"\x00\xffab"


@pytest.mark.parametrize("value", [[{"name": "a"}], [[{"id": 1}]], [{"id": 1}, {"id": 1}]])
def test_unidentifiable_and_nested_lists_are_atomic_conflicts(value):
    conflicts = []
    assert _rebase({"rows": []}, {"rows": value}, {"rows": ["peer"]}, conflicts) == {"rows": value}
    assert conflicts == ["/rows"]


def test_nested_record_lists_and_numeric_dictionary_keys_merge_without_mutation():
    base = {"7": {"groups": [{"id": 1, "children": [{"id": 2, "a": 0, "b": 0}]}]}}
    incoming, current = deepcopy(base), deepcopy(base)
    incoming["7"]["groups"][0]["children"][0]["a"] = 1
    current["7"]["groups"][0]["children"][0]["b"] = 2
    conflicts = []
    result = _rebase(base, incoming, current, conflicts)
    assert result["7"]["groups"][0]["children"] == [{"id": 2, "a": 1, "b": 2}]
    assert current["7"]["groups"][0]["children"][0]["a"] == 0
    assert conflicts == []


def test_compressed_state_response_is_rewritten(tmp_path):
    import gzip

    body = gzip.compress(json.dumps({"stored_state": STATE}).encode())
    with (
        response_proxy(
            tmp_path, body=body, content_type="application/json", extra=[("Content-Encoding", "gzip")]
        ) as (origin, _),
        urlopen(origin + "/state", timeout=5) as response,
    ):
        assert response.headers.get("Content-Encoding") is None
        served = json.load(response)["stored_state"]["currentUser"]
        assert {k: v for k, v in served.items() if k != "avatar"} == USERS[1]


def test_parallel_attribution_records_remain_complete_json(tmp_path):
    with workers(tmp_path) as (_, clients, states, log, _):
        # Large Unicode records exercise append encoding/buffering as well as locking.
        def save(index):
            state = deepcopy(states[index % 2])
            state["evidence-" + "é" * 9000] = index
            return clients[index % 2].update(SID, state)

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(result["success"] for result in pool.map(save, range(12)))
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(entries) == 12
        assert sorted(entry["worker_id"] for entry in entries) == ["w1"] * 6 + ["w2"] * 6
        assert all(entry["sid"] == SID and len(entry["changed_keys"][0]) > 9000 for entry in entries)


def test_worker_id_cannot_terminate_bootstrap_script(tmp_path):
    stub = StubHub()
    try:
        with WorkerAppProxy(
            stub.url, worker_id="</script><script>attack()</script>", sid=SID, host="127.0.0.1"
        ) as proxy:
            with urlopen(f"http://127.0.0.1:{proxy.port}/", timeout=5) as response:
                page = response.read()
            assert page.count(b"</script>") == 1
            assert b"\\u003c/script>" in page
    finally:
        stub.close()


@pytest.mark.parametrize("generic", [True, False])
def test_proxy_rejects_ambiguous_request_framing(tmp_path, generic):
    import http.client

    with response_proxy(tmp_path, generic=generic) as (origin, calls):
        conn = http.client.HTTPConnection(origin.removeprefix("http://"), timeout=5)
        try:
            conn.putrequest("POST", "/post")
            conn.putheader("Content-Length", "0")
            conn.putheader("Content-Length", "99")
            conn.endheaders()
            response = conn.getresponse()
            assert response.status == 400
            response.read()
            assert calls == []
        finally:
            conn.close()


@pytest.mark.parametrize("generic", [True, False])
def test_proxy_preserves_repeated_response_headers(tmp_path, generic):
    extra = [
        ("Set-Cookie", "one=1"),
        ("Set-Cookie", "two=2"),
        ("Content-Security-Policy", "default-src 'self'"),
        ("Content-Security-Policy", "frame-ancestors 'none'"),
    ]
    with (
        response_proxy(tmp_path, extra=extra, generic=generic) as (origin, _),
        urlopen(origin, timeout=5) as response,
    ):
        response.read()
        assert response.headers.get_all("Set-Cookie") == ["one=1", "two=2"]
        policies = response.headers.get_all("Content-Security-Policy")
        assert len(policies) == 2
        assert "default-src 'self'" in policies[0]
        assert policies[1] == "frame-ancestors 'none'"


def _refused(client, state):
    with pytest.raises(HTTPError) as refused:
        client.update(SID, state)
    assert refused.value.code == 409
    return json.loads(refused.value.read())


def test_a_save_that_replaces_every_record_with_unrelated_ids_is_refused(tmp_path):
    """A tab that never loaded the seed saves its demo data: no shared id, any size."""
    with workers(tmp_path) as (shared, clients, states, log, _):
        demo = {**states[0], "tickets": [{"id": 101, "status": "open", "subject": "Demo ticket"}]}
        error = _refused(clients[0], demo)
        assert "unrelated records" in error["error"] and "refused" in error["error"]
        assert error["collections"] == [{"collection": "tickets", "before": 1, "incoming": 1}]
        assert shared.current(SID)["stored_state"] == STATE
        assert not log.exists()
        # Keyed maps of records are collections too (Docs keeps documents by id).
        stored = {**STATE, "documents": {"d1": {"id": "d1"}, "d2": {"id": "d2"}}}
        replaced = {**stored, "documents": {"doc-1": {"id": "doc-1"}, "doc-2": {"id": "doc-2"}}}
        assert hub_identity.refusal(stored, replaced)["collections"] == [
            {"collection": "documents", "before": 2, "incoming": 2}
        ]


def test_a_save_that_drops_top_level_keys_is_refused(tmp_path):
    with workers(tmp_path) as (shared, clients, states, log, _):
        partial = {key: value for key, value in states[0].items() if key != "comments"}
        error = _refused(clients[0], partial)
        assert error["keys"] == ["comments"] and "refused" in error["error"]
        assert shared.current(SID)["stored_state"] == STATE
        assert not log.exists()


def test_ordinary_edits_are_still_accepted(tmp_path):
    with workers(tmp_path) as (shared, clients, states, log, _):
        # Delete the only ticket: an emptied collection is editing, not a replacement.
        clients[0].update(SID, {**states[0], "tickets": []})
        assert shared.current(SID)["stored_state"]["tickets"] == []
        # Recreate one and add another, keeping a shared id across the next save.
        refreshed = clients[1].current(SID)["stored_state"]
        refreshed["tickets"] = [{"id": 7, "status": "reopened", "subject": "Delivery"}]
        clients[1].update(SID, refreshed)
        refreshed["tickets"].append({"id": 8, "status": "open", "subject": "Second"})
        refreshed["tickets"][0]["status"] = "closed"
        clients[1].update(SID, refreshed)
        tickets = shared.current(SID)["stored_state"]["tickets"]
        assert [(t["id"], t["status"]) for t in tickets] == [(7, "closed"), (8, "open")]
        # Replace one of two records in a save: one id survives, so it is an edit.
        refreshed["tickets"] = [refreshed["tickets"][0], {"id": 9, "status": "open", "subject": "Third"}]
        clients[1].update(SID, refreshed)
        assert {t["id"] for t in shared.current(SID)["stored_state"]["tickets"]} == {7, 9}
        assert all(json.loads(line)["worker_id"] in {"w1", "w2"} for line in log.read_text().splitlines())


def test_refusal_rules_are_ordered_and_scalar_keys_are_ignored():
    stored = {"documents": {"d1": {"id": "d1"}, "d2": {"id": "d2"}}, "ui": {"open": "d1"}, "title": "Plan"}
    assert hub_identity.refusal(stored, {**stored, "title": "Renamed", "ui": {"open": "d2"}}) is None
    assert hub_identity.refusal({}, stored) is None  # an empty store accepts its first state
    dropped = hub_identity.refusal(stored, {"documents": stored["documents"]})
    assert dropped["keys"] == ["title", "ui"]
    replaced = hub_identity.refusal(stored, {**stored, "documents": {"doc-1": {"id": "doc-1"}}})
    assert replaced["collections"] == [{"collection": "documents", "before": 2, "incoming": 1}]
    assert hub_identity.refusal(stored, {**stored, "documents": {}}) is None  # deleting both is editing
    big = {"sheets": [{"id": f"s{i}"} for i in range(6)]}
    wiped = hub_identity.refusal(big, {"sheets": big["sheets"][:1]})
    assert wiped["collections"] == [{"collection": "sheets", "before": 6, "removed": 5}]


@pytest.mark.skipif(shutil.which("node") is None, reason="Bootstrap syntax check requires node on PATH")
def test_bootstrap_script_is_valid_javascript(tmp_path):
    with (
        workers(tmp_path) as (_, _, _, _, proxies),
        urlopen(f"http://127.0.0.1:{proxies[0].port}/?sid={SID}", timeout=5) as response,
    ):
        page = response.read().decode()
    script = page.split('data-company-envs="identity">', 1)[1].split("</script>", 1)[0]
    (tmp_path / "bootstrap.js").write_text(script)
    result = subprocess.run(
        ["node", "--check", str(tmp_path / "bootstrap.js")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_an_apps_load_time_seed_is_recorded_as_a_save_and_never_reseeds(tmp_path):
    """31 of the 98 clones on disk POST ``action: "set"`` once on load to claim their baseline, 29
    of them among the 90 apps in the catalogue.

    ``set`` is the *seeding* action: it redefines the session's initial state, which is the grader's
    zero point, so a worker must never reach it. Refusing it outright was right about the store and
    wrong about the signal -- it logged a console 403 in every one of those 31 apps, so a guest-side
    "no browser faults" check reported a fault for all of them and meant nothing. The content is
    still a worker's save, so the proxy takes it as one: the action becomes ``set_current``, the
    baseline is untouched, the attribution log says the action was rebased, and ``refusal`` still
    turns away an app that is resetting to its demo data.
    """
    with workers(tmp_path) as (shared, _clients, states, log, proxies):
        before = shared.inspect(SID)
        state = deepcopy(states[0])
        state["tickets"].append({"id": 11, "status": "open", "subject": "Added on load"})
        request = Request(
            f"http://127.0.0.1:{proxies[0].port}/post?sid={SID}",
            data=json.dumps({"action": "set", "state": state, "merge": False}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            assert response.status == 200  # no console 403 for any of the 31 apps
        after = shared.inspect(SID)
        assert after["initial_state"] == before["initial_state"]  # the grader's zero point stands
        assert {t["id"] for t in after["current_state"]["tickets"]} == {7, 11}
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert entries[-1]["rebased_action"] == "set"
        assert "tickets" in entries[-1]["changed_keys"]


def test_a_load_time_seed_that_is_demo_data_is_still_refused(tmp_path):
    """The rewrite does not weaken the store: a save that replaces the world is refused as before."""
    with workers(tmp_path) as (shared, _clients, _states, _log, proxies):
        request = Request(
            f"http://127.0.0.1:{proxies[0].port}/post?sid={SID}",
            data=json.dumps({"action": "set", "state": {"currentUser": USERS[0]}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=10)
        assert caught.value.code == 409
        assert json.loads(caught.value.read())["refused"] == "dropped_keys"
        assert shared.inspect(SID)["current_state"]["tickets"] == STATE["tickets"]
