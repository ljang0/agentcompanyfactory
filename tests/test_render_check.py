"""The browser render check: verdicts on fake DOM text and state envelopes, and one real Chrome pass."""

import json
import subprocess
import sys
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar

import pytest

from company_envs.storage import read, write
from company_envs.world import render_check as rc

PAGE = """<!doctype html><html><head><title>Drive</title><style>body{color:red}</style>
<script>window.x = "Loading..."; /* not visible */</script></head>
<body><!-- a comment --><div>Q3 budget review &amp; notes</div>
<template><p>Loading</p></template><p>Second   line</p></body></html>"""

STATE = {
    "user": {"id": "u1", "name": "Ada Lovelace-Byron"},
    "files": [
        {"id": "f1", "name": "Q3 budget review & notes", "body": "x" * 100},
        {"id": "f2", "name": "Q3 budget review & notes", "tags": ["one two three"]},
        {"id": "f3", "name": "short", "note": "no-spaces-here-at-all"},
    ],
}


def test_visible_text_strips_code_blocks_comments_and_tags():
    assert rc.visible_text(PAGE) == "Drive Q3 budget review & notes Second line"
    assert rc.visible_text("") == ""
    assert rc.visible_text("<SCRIPT>a</SCRIPT ><Style>b</style>c") == "c"


def test_loading_sentinel_is_short_text_that_says_loading():
    assert rc.is_loading("Google Drive Loading...")
    assert rc.is_loading("  loading  ")
    assert not rc.is_loading("")  # empty is its own failure, not a loading screen
    assert not rc.is_loading("Inbox Loading more messages " + "x " * 80)  # a rendered app may say it
    assert not rc.is_loading("Downloading report")  # the word, not a substring


def test_seeded_strings_are_human_sized_distinct_and_capped():
    assert rc.seeded_strings(STATE) == ["Ada Lovelace-Byron", "Q3 budget review & notes", "one two three"]
    assert rc.seeded_strings({"a": "  spaced   out  words  "}) == ["spaced out words"]
    assert rc.seeded_strings({"a": [f"item number {i} here" for i in range(20)]}, cap=3) == [
        "item number 0 here",
        "item number 1 here",
        "item number 2 here",
    ]
    assert rc.strings_seen(["Q3 budget review & notes", "one two three"], rc.visible_text(PAGE)) == 1


def test_stored_state_is_unwrapped_from_the_envelope():
    assert rc.stored({"stored_state": {"a": 1}, "has_custom_state": True, "sid": "s"}) == {"a": 1}
    assert rc.stored({"a": 1}) == {"a": 1}
    assert rc.stored(None) is None


@pytest.mark.parametrize(
    ("text", "before", "after", "error", "ok", "reason"),
    [
        ("Inbox 12 messages from Ada", {"stored_state": {"a": 1}}, {"stored_state": {"a": 1}}, None, True, None),
        ("Drive Loading...", {"stored_state": {"a": 1}}, {"stored_state": {"a": 1}}, None, False, "loading screen"),
        ("", {"stored_state": {"a": 1}}, {"stored_state": {"a": 1}}, None, False, "no visible text"),
        ("Inbox 12 messages", {"stored_state": {"a": 1}}, {"stored_state": {"a": 2}}, None, True, None),
        ("Inbox 12 messages", {"stored_state": {"emails": [{"id": 1}, {"id": 2}]}}, {"stored_state": {"emails": [{"id": 2}]}}, None, False, "lost"),
        ("Inbox 12 messages", {"stored_state": {"a": 1, "labels": []}}, {"stored_state": {"a": 1}}, None, False, "vanished"),
        ("Inbox 12 messages", {"stored_state": {"a": 1}}, None, None, False, "could not be read"),
        ("Inbox 12 messages", {"stored_state": {"a": 1}}, {"stored_state": {"a": 1}}, "browser exited 1", False, "exited"),
    ],
)  # fmt: skip
def test_verdict(text, before, after, error, ok, reason):
    result = rc.verdict(text, before, after, error=error)
    assert result["ok"] is ok and result["text_chars"] == len(text)
    assert result["loading"] is (text == "Drive Loading...")
    assert result["state_unchanged"] is (after is not None and before == after)
    if ok:
        assert result["error"] is None
    else:
        assert reason in result["error"]


def test_worker_url_uses_the_first_worker_and_a_reachable_host():
    entry = {
        "workers": {"boss": "http://0.0.0.0:4123/?sid=acme-ep1", "clerk": "http://0.0.0.0:4124/?sid=acme-ep1"}
    }
    assert rc.worker_url(entry) == (
        "http://127.0.0.1:4123/?sid=acme-ep1",
        "http://127.0.0.1:4123/state?sid=acme-ep1",
    )
    assert rc.worker_url(entry, "10.0.0.5")[0] == "http://10.0.0.5:4123/?sid=acme-ep1"
    page, state = rc.worker_url({"workers": {"w": "http://127.0.0.1:80/app/?sid=s&x=1"}})
    assert (page, state) == ("http://127.0.0.1:80/app/?sid=s&x=1", "http://127.0.0.1:80/state?sid=s")


def test_find_browser_honors_the_environment_variable(tmp_path, monkeypatch):
    fake = tmp_path / "my-chrome"
    fake.write_text("")
    assert rc.find_browser({"COMPANY_ENVS_BROWSER": str(fake)}) == str(fake)
    monkeypatch.setattr(rc.shutil, "which", lambda name: f"/bin/{name}" if name == "chromium" else None)
    assert rc.find_browser({"COMPANY_ENVS_BROWSER": "chromium"}) == "/bin/chromium"
    assert rc.find_browser({"COMPANY_ENVS_BROWSER": "nothing-here"}) is None
    assert rc.find_browser({}) == "/bin/chromium"
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    assert rc.find_browser({}) is None


def test_render_dom_reports_timeouts_exits_and_missing_binaries(monkeypatch, tmp_path):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(rc.subprocess, "run", timeout)
    assert rc.render_dom("chrome", "http://x/", profile=tmp_path, timeout=7) == (
        "",
        "browser timed out after 7s",
    )
    monkeypatch.setattr(
        rc.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=133, stdout="<p>partial</p>", stderr="\nboom\n"),
    )
    assert rc.render_dom("chrome", "http://x/", profile=tmp_path) == (
        "<p>partial</p>",
        "browser exited 133: boom",
    )
    seen = {}

    def ok(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="<p>fine</p>", stderr="")

    monkeypatch.setattr(rc.subprocess, "run", ok)
    assert rc.render_dom("chrome", "http://x/?sid=s", profile=tmp_path / "p") == ("<p>fine</p>", None)
    assert seen["command"][0] == "chrome" and seen["command"][-2:] == ["--dump-dom", "http://x/?sid=s"]
    assert f"--user-data-dir={tmp_path / 'p'}" in seen["command"] and "--headless=new" in seen["command"]
    monkeypatch.undo()  # the real subprocess.run: a binary that does not exist cannot start
    dom, error = rc.render_dom(str(tmp_path / "missing-browser"), "http://x/", profile=tmp_path)
    assert dom == "" and error.startswith("browser could not start")


def company(tmp_path, apps=("drive_mock", "gmail_mock")):
    folder = tmp_path / "companies" / "acme"
    endpoints = {}
    for index, app in enumerate(apps):
        write(folder / "world" / f"{app}.state.json", STATE)
        endpoints[app] = {
            "harness_url": f"http://127.0.0.1:{9000 + index}",
            "state_file": f"world/{app}.state.json",
            "identity_key": "user",
            "workers": {"boss": f"http://0.0.0.0:{7000 + index}/?sid=acme-ep1"},
        }
    write(folder / "runtime" / "endpoints.json", {"apps": endpoints})
    return folder


def test_check_folder_renders_each_app_once_and_writes_the_report(tmp_path):
    folder = company(tmp_path)
    calls = []
    envelope = {"stored_state": STATE, "has_custom_state": True, "sid": "acme-ep1"}

    def render(browser, url, *, profile, **_kw):
        calls.append(("render", browser, url))
        assert profile.startswith(str(tmp_path / "work")) and (tmp_path / "work").is_dir()
        return (PAGE, None) if ":7000/" in url else ("<title>Gmail</title><body>Loading...</body>", None)

    def fetch(url):
        calls.append(("fetch", url))
        return envelope

    report = rc.run_render_check(
        folder, work=tmp_path / "work", browser="/bin/chrome", settle=0, render=render, fetch=fetch
    )
    assert report == read(folder / "runtime" / "RENDER.json")
    assert report["ok"] is False and report["browser"] == "/bin/chrome" and report["at"].endswith("+00:00")
    drive, gmail = report["apps"]["drive_mock"], report["apps"]["gmail_mock"]
    assert drive == {
        "ok": True, "loading": False, "text_chars": len("Drive Q3 budget review & notes Second line"),
        "state_unchanged": True, "state_change": None, "state_added": [], "error": None,
        "seeded_strings_seen": "1/3", "route": "/",
        "screenshot": None, "controls": 0, "broken_images": [], "empty_images": 0, "placeholders": [],
    }  # fmt: skip
    assert gmail["ok"] is False and gmail["loading"] is True and gmail["seeded_strings_seen"] == "0/3"
    assert gmail["error"].startswith("loading screen")
    assert calls == [
        ("fetch", "http://127.0.0.1:7000/state?sid=acme-ep1"),
        ("render", "/bin/chrome", "http://127.0.0.1:7000/?sid=acme-ep1"),
        ("fetch", "http://127.0.0.1:7000/state?sid=acme-ep1"),
        ("fetch", "http://127.0.0.1:7001/state?sid=acme-ep1"),
        ("render", "/bin/chrome", "http://127.0.0.1:7001/?sid=acme-ep1"),
        ("fetch", "http://127.0.0.1:7001/state?sid=acme-ep1"),
    ]
    assert not list((tmp_path / "work").iterdir())  # throwaway profiles are removed
    lines = rc.format_lines(report)
    assert lines[0] == "PASS drive_mock: 42 visible chars, seeded strings seen 1/3, state unchanged"
    assert lines[1].startswith(
        "FAIL gmail_mock: 16 visible chars, seeded strings seen 0/3, state unchanged -- loading"
    )
    assert lines[2] == "render-check FAILED: 2 apps, failed gmail_mock"


def test_a_visit_that_writes_back_or_cannot_read_the_state_fails(tmp_path):
    folder = company(tmp_path, apps=("drive_mock",))
    envelopes = iter(
        [{"stored_state": {"a": 1, "rows": [{"id": 1}]}}, {"stored_state": {"a": 1, "b": 2, "rows": []}}]
    )
    report = rc.check_folder(
        folder,
        browser="/bin/chrome",
        settle=0,
        render=lambda *a, **k: (PAGE, None),
        fetch=lambda url: next(envelopes),
    )
    result = report["apps"]["drive_mock"]
    assert report["ok"] is False and result["state_unchanged"] is False and "lost" in result["error"]

    def refuse(url):
        raise OSError("connection refused")

    report = rc.check_folder(
        folder, browser="/bin/chrome", settle=0, render=lambda *a, **k: (PAGE, None), fetch=refuse
    )
    assert report["apps"]["drive_mock"]["error"] == "state fetch failed: connection refused"
    assert report["apps"]["drive_mock"]["seeded_strings_seen"] == "0/3"
    # A browser failure is the error even when some DOM came out.
    report = rc.check_folder(
        folder,
        browser="/bin/chrome",
        settle=0,
        render=lambda *a, **k: (PAGE, "browser exited 1"),
        fetch=lambda url: {"stored_state": {}},
    )
    assert report["apps"]["drive_mock"] == {
        "ok": False, "loading": False, "text_chars": 42, "state_unchanged": True, "state_change": None,
        "state_added": [], "screenshot": None, "route": "/",
        "controls": 0, "broken_images": [], "empty_images": 0, "placeholders": [],
        "error": "browser exited 1", "seeded_strings_seen": "1/3",
    }  # fmt: skip


def test_a_host_that_cannot_render_is_a_fault_and_is_still_admitted(tmp_path, monkeypatch):
    """It used to be `ok: True` -- the same value a render of every app passing writes -- and that is
    the defect class in one line: a run that learned nothing about the world wore the receipt of one
    that proved it.

    It is a **fault**, not an unmeasured run: the inputs were all there and the environment failed.
    The distinction earns its keep at the gate. A fault the environment cannot clear is not retried
    -- no stage installs a browser, and retrying would be a permanent stall on a defect the pipeline
    cannot repair -- so ``admissible`` lets it through and the VM stage takes the measurement again
    in a real browser. A render that measured nothing does not get that pass, which is the whole
    point of separating the two.
    """
    folder = company(tmp_path)
    monkeypatch.setattr(rc, "find_browser", lambda env=None: None)
    report = rc.run_render_check(folder)
    assert report["browser"] is None and report["apps"] == {}
    assert report["outcome"] == "faulted" and report["ok"] is False
    assert report["skipped"].startswith("no headless browser found")
    assert rc.admissible(report) is True, "the host failed; the company is not blamed for it"
    assert read(folder / "runtime" / "RENDER.json") == report
    assert rc.format_lines(report) == [f"SKIP render-check: {rc.NO_BROWSER}"]


def test_cli_prints_one_line_per_app_and_exits_1_on_failure(tmp_path, monkeypatch, capsys):
    from company_envs.__main__ import main

    folder = company(tmp_path, apps=("drive_mock",))
    seen = {}

    def fake(path, *, host, work):
        seen.update(folder=path, host=host, work=work)
        return {"ok": False, "at": "t", "browser": "/bin/chrome", "apps": {"drive_mock": {
            "ok": False, "loading": True, "text_chars": 16, "state_unchanged": True, "state_change": None, "screenshot": None,
            "controls": 0, "broken_images": [], "empty_images": 0, "placeholders": [],
            "error": "loading screen: 'Drive Loading...'", "seeded_strings_seen": "0/2"}}}  # fmt: skip

    monkeypatch.setattr(rc, "run_render_check", fake)
    monkeypatch.setattr(sys, "argv", ["company-envs", "render-check", str(folder), "--host", "10.0.0.5"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1 and seen == {"folder": folder, "host": "10.0.0.5", "work": None}
    out = capsys.readouterr().out.splitlines()
    assert (
        out[0].startswith("FAIL drive_mock: 16 visible chars")
        and out[1] == "render-check FAILED: 1 apps, failed drive_mock"
    )


class Hub(BaseHTTPRequestHandler):
    """A one-app stand-in for a worker proxy: / is the page, /state the envelope."""

    page: ClassVar[str] = ""
    state: ClassVar[dict] = {}
    log: ClassVar[list] = []

    def do_GET(self):
        self.log.append(self.path)
        if self.path.startswith("/state"):
            body = json.dumps({"stored_state": self.state, "has_custom_state": True, "sid": "s"}).encode()
            kind = "application/json"
        elif self.path.startswith("/?"):
            body, kind = self.page.encode(), "text/html"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HYDRATING = """<!doctype html><html><head><title>Drive</title></head><body><div id="root">Loading...</div>
<script>
fetch('/state?sid=s').then(r => r.json()).then(d => {
  const root = document.getElementById('root'); root.textContent = '';
  for (const f of d.stored_state.files) { const p = document.createElement('p'); p.textContent = f.name; root.appendChild(p); }
});
</script></body></html>"""

STUCK = """<!doctype html><html><head><title>Drive</title></head><body><div id="root">Loading...</div>
<script>fetch('/state?sid=s').then(() => { throw new Error('QuotaExceededError'); });</script></body></html>"""


@pytest.mark.skipif(rc.find_browser() is None, reason="no headless Chrome or Chromium on this host")
@pytest.mark.parametrize("page", ["HYDRATING", "STUCK"])
def test_real_browser_renders_a_hydrating_page_and_catches_a_stuck_one(tmp_path, page):
    handler = type("Handler", (Hub,), {"page": globals()[page], "state": STATE, "log": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        folder = tmp_path / "companies" / "acme"
        write(folder / "world" / "drive_mock.state.json", STATE)
        entry = {
            "state_file": "world/drive_mock.state.json",
            "workers": {"w": f"http://0.0.0.0:{server.server_port}/?sid=s"},
        }
        write(folder / "runtime" / "endpoints.json", {"apps": {"drive_mock": entry}})
        render = partial(rc.render_dom, timeout=90, virtual_time_ms=3000)
        report = rc.run_render_check(folder, work=tmp_path / "work", settle=0, render=render)
    finally:
        server.shutdown()
        server.server_close()
    result = report["apps"]["drive_mock"]
    assert handler.log.count("/state?sid=s") >= 3  # the check's two reads and the page's own load
    if page == "HYDRATING":
        assert result["ok"] is True and result["seeded_strings_seen"] == "1/3", result
        assert result["loading"] is False and result["state_unchanged"] is True
    else:
        assert (
            result["ok"] is False
            and result["loading"] is True
            and result["error"].startswith("loading screen")
        )


def test_a_visit_that_only_fills_in_defaults_passes_with_the_change_recorded():
    before = {"stored_state": {"events": [{"id": "e1", "status": "confirmed", "meetLink": "x"}]}}
    after = {"stored_state": {"events": [{"id": "e1"}], "settings": {"density": "default"}}}
    result = rc.verdict("Calendar September", before, after)
    assert result["ok"] is True and result["state_unchanged"] is False
    assert result["state_change"] == "+settings ~events[meetLink,status]"


def test_a_first_view_that_opens_on_a_failure_fails_without_a_model():
    assert rc.failure_text("Channel not found") == "not found"
    assert rc.failure_text("Something went wrong loading your inbox") == "Something went wrong"
    # A business record that happens to use the word is not a failure: only the opening is judged.
    assert rc.failure_text("Inbox. Re: error report from Ada. Re: 404 pallets short.") is None
    assert rc.failure_text("x" * 500 + " not found") is None


def test_template_and_leaked_text_are_found_anywhere_in_the_view():
    assert rc.placeholder_text("Recent: Marketing Campaign Tracker, Alex Johnson") == ["Alex Johnson"]
    assert rc.placeholder_text("Total: NaN") == ["NaN"]
    assert rc.placeholder_text("Owner [object Object]") == ["[object Object]"]
    assert rc.placeholder_text("Celia Renshaw approved the September plan") == []


def test_an_image_the_proxy_did_not_rewrite_fails_the_app(monkeypatch):
    """The proxy replaces every unreachable image; one surviving to the page is a regression."""
    app = "<div>" + "seeded " * 400 + '</div><button>x</button><img src="https://cdn.example/a.png">'
    result = rc.verdict("Inbox", {"stored_state": {}}, {"stored_state": {}}, dom=app)
    assert not result["ok"] and result["broken_images"] == ["https://cdn.example/a.png"]
    assert "cannot draw" in result["error"]
    monkeypatch.setattr(rc, "BLOCK_BROKEN_IMAGES", False)  # the escape hatch still reports them
    lenient = rc.verdict("Inbox", {"stored_state": {}}, {"stored_state": {}}, dom=app)
    assert lenient["ok"] and lenient["broken_images"] == ["https://cdn.example/a.png"]


def test_images_a_browser_cannot_draw_are_counted():
    dom = (
        '<img src="">'
        '<img src="/assets/logo.png">'
        '<img src="data:image/svg+xml;base64,abc">'
        '<img src="https://ui-avatars.com/api/?name=Alex+Johnson">'
    )
    assert rc.broken_images(dom) == ["https://ui-avatars.com/api/?name=Alex+Johnson"]
    assert rc.broken_images("<p>no images</p>") == []
    # An empty source is the clone's own default: counted for the judge to weigh, never blocking.
    assert rc.empty_images(dom) == 1 and rc.empty_images("<p>none</p>") == 0


def test_a_rendered_page_with_nothing_to_click_fails_but_a_stub_does_not():
    app = "<div>" + "seeded content " * 200 + "</div>"
    assert len(app) > rc.APP_DOM
    result = rc.verdict("Inbox", {"stored_state": {}}, {"stored_state": {}}, dom=app)
    assert not result["ok"] and "clicked" in result["error"] and result["controls"] == 0
    working = app + "<button>Compose</button>"
    assert rc.verdict("Inbox", {"stored_state": {}}, {"stored_state": {}}, dom=working)["ok"]
    stub = "<p>short</p>"  # a test double or a tiny page is not judged on affordances
    assert rc.verdict("Inbox", {"stored_state": {}}, {"stored_state": {}}, dom=stub)["ok"]


def app_dom(text, controls=1):
    """A DOM the size of a real app screen, carrying TEXT: below APP_DOM nothing is judged."""
    return f"<div>{text}</div><div hidden>{'x' * rc.APP_DOM}</div>" + "<button>Go</button>" * controls


def test_a_first_view_that_shows_almost_none_of_the_world_fails():
    """render_ok ignored seeded_strings_seen outright -- the docstring said so.

    Measured over the 20 rendered companies: 71 of 132 passing app renders showed under a tenth
    of their seeded strings, google_drive_mock passing in all 20 on 1 of 400 strings and ~210
    visible characters, google_sheets_mock on 1 of 170 and 178 characters.
    """
    drive = "My Drive Name Owner Last modified NJ-014 store records me Sep 6, 2026 Storage"
    result = rc.verdict(
        drive, {"stored_state": {}}, {"stored_state": {}}, dom=app_dom(drive), seen=1, total=400
    )
    assert result["ok"] is False
    assert result["error"].startswith("the first view shows 1 of 400 of this company's own values")
    # Five of the company's own values is a workspace, whatever the ratio: a big world cannot be
    # asked for a percentage of itself (gmail's inbox shows 47 of 400 sampled subjects).
    assert rc.verdict(
        drive, {"stored_state": {}}, {"stored_state": {}}, dom=app_dom(drive), seen=5, total=400
    )["ok"]


def test_the_floor_spares_a_page_whose_text_the_matcher_cannot_reach():
    """A seeded string is 12-60 characters matched exactly, so truncation and long messages miss.

    Measured: california-civil-rights-department's slack renders 16,852 characters of the
    company's own conversation and matches 4 of its 22 sampled strings. Above FLOOR_TEXT the
    shortfall is the matcher, not the page.
    """
    wordy = "Amaya Rios " + "the accommodation chronology and payroll clarification matters " * 40
    assert len(wordy) >= rc.FLOOR_TEXT
    assert rc.content_floor(4, 22, len(wordy), app_dom(wordy)) is None
    assert rc.content_floor(4, 22, rc.FLOOR_TEXT - 1, app_dom("short")) is not None
    # A state with no human-sized values, and a stub too small to be an app screen, are exempt.
    assert rc.content_floor(0, 0, 10, app_dom("x")) is None
    assert rc.content_floor(0, 400, 10, "<p>stub</p>") is None
    # Fewer seeded values than the floor: the world is asked for what it has, not for five.
    assert rc.content_floor(3, 3, 40, app_dom("x")) is None


def test_the_gate_opens_the_route_the_app_keeps_the_world_on():
    """Both gates only ever opened `/`, and for several apps `/` is a launcher.

    Measured on one real company world each: sheets' `/` shows 1 of 400 seeded strings in 199
    characters and `/spreadsheet` 54 in 2,489; drive's `/` 1 of 400 in 217 and `/recent` 21 in
    1,322; hubspot's `/` 2 of 400 in 1,288 and `/contacts` 24 in 2,461.
    """
    entry = {"workers": {"boss": "http://0.0.0.0:4123/?sid=acme-ep1"}}
    assert rc.worker_url(entry, app_id="google_sheets_mock")[0] == (
        "http://127.0.0.1:4123/spreadsheet?sid=acme-ep1"
    )
    assert rc.worker_url(entry, app_id="google_drive_mock")[0].endswith("/recent?sid=acme-ep1")
    assert rc.worker_url(entry, app_id="gmail_mock")[0].endswith("4123/?sid=acme-ep1")
    # The endpoint's own record wins, so hub_world can take the fact over without a code change,
    # and so does a path already in the proxy URL.
    recorded = {"entry_path": "/inbox", **entry}
    assert rc.worker_url(recorded, app_id="google_sheets_mock")[0].endswith("/inbox?sid=acme-ep1")
    served = {"workers": {"boss": "http://0.0.0.0:4123/mail/?sid=s"}}
    assert rc.worker_url(served, app_id="google_sheets_mock")[0].endswith("/mail/?sid=s")
    assert rc.entry_route({}, "nothing_mock") == "/"


def test_check_app_opens_the_entry_route_and_records_it(tmp_path):
    folder = company(tmp_path, apps=("google_sheets_mock",))
    seen = {}

    def render(browser, url, *, profile, **_kw):
        seen["url"] = url
        return "<p>Sheets</p>", None

    report = rc.check_folder(
        folder, browser="/bin/chrome", settle=0, render=render, fetch=lambda url: {"stored_state": STATE}
    )
    assert seen["url"] == "http://127.0.0.1:7000/spreadsheet?sid=acme-ep1"
    assert report["apps"]["google_sheets_mock"]["route"] == "/spreadsheet"
    assert rc.format_lines(report)[0].endswith("state unchanged, route /spreadsheet")


def test_a_visit_that_adds_the_app_s_own_records_fails():
    """losses() checked only keys that vanished, never keys the app added.

    Measured: clio passed with state_change "+trustAccounts +trustTransactions +onlinePayments
    +appIntegrations" -- the clone hydrating its shipped defaults over a law firm's seed, three
    trust accounts at the Royal Bank of Canada and six transactions naming another firm's
    matters. A key the app fills in that carries no records is not that: gmail adds `settings` in
    all 20 rendered companies, facebook three empty lists, hubspot an empty `emails`.
    """
    seed = {"matters": [{"id": "m1"}]}
    demo = {
        "matters": [{"id": "m1"}],
        "trustAccounts": [{"id": "ta-1", "bank": "Royal Bank of Canada"}, {"id": "ta-2"}],
        "onlinePayments": {"enabled": True, "paymentLinks": [{"id": "pl-1"}]},
    }
    assert rc.added_records(seed, demo) == [
        "trustAccounts arrived holding 2 record(s) the seed never had",
        "onlinePayments arrived holding 1 record(s) the seed never had",
    ]
    result = rc.verdict("Matters", {"stored_state": seed}, {"stored_state": demo}, dom=app_dom("Matters"))
    assert result["ok"] is False and result["error"].startswith("a read-only visit added the app's own")
    assert len(result["state_added"]) == 2
    # Defaults that carry no records still pass, and are still reported as a change.
    filled = {"matters": [{"id": "m1"}], "settings": {"density": "default"}, "hiddenPosts": []}
    assert rc.added_records(seed, filled) == []
    kept = rc.verdict("Matters", {"stored_state": seed}, {"stored_state": filled}, dom=app_dom("Matters"))
    assert kept["ok"] is True and kept["state_change"] == "+settings +hiddenPosts"


def test_the_report_publishes_the_waits_a_guest_needs(tmp_path):
    """A guest hydrates about three times slower than this host, and this gate never runs in one.

    Measured on the host: --virtual-time-budget fast-forwards idle time, so hubspot's /deals
    dumps a byte-identical DOM (11,202 visible characters, 247 of 400 seeded strings) at a
    3-second and a 30-second budget. The number a guest needs is published, not applied here.
    """
    folder = company(tmp_path, apps=("drive_mock",))
    report = rc.check_folder(
        folder,
        browser="/bin/chrome",
        settle=0,
        render=lambda *a, **k: (PAGE, None),
        fetch=lambda url: {"stored_state": STATE},
    )
    assert report["settle_seconds"] == 0 and report["virtual_time_ms"] == rc.VIRTUAL_TIME_MS
    assert report["guest_settle_seconds"] == 0
    assert rc.GUEST_SLOWDOWN == 3


def test_a_render_of_no_apps_says_unmeasured_and_a_skipped_one_says_which(tmp_path, monkeypatch):
    """The same `{"ok": True}` seeded then ANDed per app: with no endpoints it was a render **pass**
    over a world nothing had been served from.

    The two empty renders are different runs and now say so. A host without Chrome is a `faulted`
    run -- the environment failed -- and `admissible` lets it through because no stage can install a
    browser. Zero endpoints with a working browser is `unmeasured`, and it stops the company, because
    a world nothing was served from has not been looked at. Measured 2026-09-10, this is a latent
    hole and not a live one: 0 of the 20 RENDER.json on disk rendered zero apps, and 0 of the 51
    endpoints.json held zero apps.
    """
    from company_envs.storage import read, write
    from company_envs.world.render_check import check_folder, run_render_check

    folder = tmp_path / "company"
    (folder / "runtime").mkdir(parents=True)
    write(folder / "runtime/endpoints.json", {"apps": {}})
    monkeypatch.setattr(rc, "find_browser", lambda: None)
    skipped = check_folder(folder, browser=None)
    assert skipped["outcome"] == "faulted", "the host failed, which says nothing about the world"
    assert rc.admissible(skipped) is True, "and no stage can install a browser, so it is admitted"

    report = run_render_check(folder, browser="/usr/bin/true")
    assert report["apps"] == {}
    assert report["outcome"] == "unmeasured", "no app was rendered, so no app was proved"
    assert report["ok"] is None and "skipped" not in report
    assert rc.admissible(report) is False, "a company nobody looked at must not reach the VM stage"
    assert read(folder / "runtime/RENDER.json")["outcome"] == "unmeasured"


def test_a_render_verdict_records_the_code_that_decided_it_including_the_empty_ones(tmp_path, monkeypatch):
    """Every marker on disk predated every fix of the night, which is the only reason this matters:
    PROXY_CODE omitted hub_app.py, so the storage-quota fix that stopped 76 of 88 apps falling back to
    their demo rows could never restale a RENDER.json.

    The list is longer than this file because the verdict is longer than this file: the browser is
    handed a page the proxy builds, so hub_app.py's shim, hub_identity.py's choice of person and
    placeholder_images.py's drawing all decide what this gate reports. Every path must exist --
    `decided_by` digests a missing path as its absence rather than raising, so a typo would be a
    silent permanent freshness, which is the failure mode this test exists for.

    The skipped and empty reports carry it too. A render that proves nothing must not also be the one
    render that never reads as stale.
    """
    from company_envs.storage import decided_by

    assert [p.name for p in rc.DECIDES_THIS] == [
        "render_check.py",
        "hub_app.py",
        "hub_identity.py",
        "placeholder_images.py",
    ]
    assert all(p.is_file() for p in rc.DECIDES_THIS), "a typo digests as absence and never restales"
    assert rc.current({}) is False and rc.current(None) is False, "no digest is stale: the migration"
    assert rc.current({"decided_by": decided_by(rc.DECIDES_THIS)}) is True
    # Any one of the four changing is staleness, which is the whole point of naming four.
    for omitted in rc.DECIDES_THIS:
        others = tuple(p for p in rc.DECIDES_THIS if p != omitted)
        assert rc.current({"decided_by": decided_by(others)}) is False, f"{omitted.name} decides this"

    folder = tmp_path / "company"
    (folder / "runtime").mkdir(parents=True)
    write(folder / "runtime/endpoints.json", {"apps": {}})
    monkeypatch.setattr(rc, "find_browser", lambda: None)
    assert rc.current(rc.check_folder(folder, browser=None)) is True, "the host-fault report carries it"
    assert rc.current(rc.run_render_check(folder, browser="/usr/bin/true")) is True, "and the empty one"
