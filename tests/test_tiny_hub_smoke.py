"""Real Node hub contract smoke; only npm installation is a local-copy double."""

import copy
import json
import shutil
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from company_envs.world import hub_app

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_hub_app"
APP = {
    "id": "tiny_hub_app",
    "source": hub_app.HUB_SOURCE,
    "schema": "SCHEMA.md",
    "state_keys": ["currentUser", "items", "ui"],
}
STATE = {
    "currentUser": {"id": "worker1", "name": "Casey"},
    "items": [{"id": "item1", "title": "Seeded inspection item 7391", "done": False}],
    "ui": {"view": "items", "filters": {"open": True}},
}
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Tiny hub smoke requires node on PATH")


@pytest.fixture
def local_install(monkeypatch):
    """Keep build() intact, substituting only its unconditional npm install."""
    real_run = subprocess.run
    installs = []

    def run(command, *args, **kwargs):
        if command[0] == "npm":
            assert command == ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]
            target = Path(kwargs["cwd"])
            assert not (target / "node_modules").exists()
            shutil.copytree(FIXTURE / "node_modules", target / "node_modules")
            installs.append(target)
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(hub_app.subprocess, "run", run)
    return installs


def test_tiny_hub_smoke(tmp_path, monkeypatch, local_install):
    rendered = []
    real_render = hub_app.render_check

    def render(url, expect):
        result = real_render(url, expect)
        rendered.append(result)
        return result

    monkeypatch.setattr(hub_app, "render_check", render)
    report = hub_app.smoke(
        APP,
        FIXTURE,
        tmp_path,
        copy.deepcopy(STATE),
        schema_text=(FIXTURE / "SCHEMA.md").read_text(),
        expect_text=STATE["items"][0]["title"],
    )
    assert report["passed"] is True
    expected_checks = {
        "seed_is_initial_state",
        "seed_is_current_state",
        "no_diff_after_seed",
        "initial_preserved_after_update",
        "diff_visible_after_update",
        "worker_proxy_denies_harness_routes",
        "worker_proxy_allows_app_saves",
        "worker_proxy_allows_app_state_load",
        "worker_proxy_serves_app",
        "reset_clears_session",
    }
    assert expected_checks <= report["checks"].keys()
    assert all(value is True for value in report["checks"].values())
    assert len(rendered) == 1
    assert rendered[0] is None or rendered[0] is True
    # smoke serializes the None result as a human-readable skip reason.
    assert report["render_check"] == ("skipped_no_chrome" if rendered[0] is None else True)
    built = tmp_path / APP["id"]
    assert local_install == [built]
    # The served page is the app's own page plus the storage shim, and nothing else.
    served = (built / "dist" / "index.html").read_text()
    assert hub_app.STORAGE_SHIM in served
    assert served.replace(hub_app.STORAGE_SHIM, "", 1) == (FIXTURE / "index.html").read_text()
    assert not (built / ".mock-states").exists()
    assert not (built / ".mock-files").exists()


def test_tiny_hub_nested_diff_sessions_and_upload(tmp_path, local_install):
    built = hub_app.build(FIXTURE, tmp_path / APP["id"])
    with hub_app.HubProcess(built) as server:
        client = server.client
        client.seed("first", STATE)
        client.seed("second", STATE)
        changed = copy.deepcopy(STATE)
        changed["items"][0]["done"] = True
        changed["ui"]["filters"] = {"owner": "worker1"}
        client.update("first", changed)
        inspected = client.inspect("first")
        assert inspected["initial_state"] == STATE
        assert inspected["current_state"] == changed
        assert inspected["state_diff"] == {
            "items": {"0": {"done": {"old": False, "new": True}}},
            "ui": {"filters": {"open": {"removed": True}, "owner": {"added": "worker1"}}},
        }
        assert client.current("second")["stored_state"] == STATE
        stored = json.loads((built / ".mock-states" / "first.json").read_text())
        assert stored == {"initial": STATE, "current": changed}

        with hub_app.WorkerProxy(client.base_url, host="127.0.0.1") as proxy:
            origin = f"http://127.0.0.1:{proxy.port}"
            payload = b"binary evidence\x00\xff"
            multipart = (
                b'--tiny-boundary\r\nContent-Disposition: form-data; name="file"; filename="proof.bin"\r\n'
                b"Content-Type: application/octet-stream\r\n\r\n" + payload + b"\r\n--tiny-boundary--\r\n"
            )
            request = Request(
                origin + "/upload?sid=first",
                data=multipart,
                headers={"Content-Type": "multipart/form-data; boundary=tiny-boundary"},
            )
            with urlopen(request, timeout=10) as response:
                uploaded = json.load(response)
            assert uploaded["success"] is True
            assert uploaded["files"][0]["size"] == len(payload)
            with urlopen(origin + uploaded["files"][0]["url"], timeout=10) as response:
                assert response.read() == payload

        client.reset("first")
        assert client.current("first")["has_custom_state"] is False
        assert not (built / ".mock-states" / "first.json").exists()
        assert client.current("second")["stored_state"] == STATE
        client.seed("second", changed)
        assert client.inspect("second")["initial_state"] == changed
        assert client.inspect("second")["state_diff"] == {}
    assert local_install == [built]
    assert not (built / ".mock-states").exists()
    assert not (built / ".mock-files").exists()


@pytest.mark.parametrize(
    ("answer", "accepted"),
    [
        ({"success": True}, True),
        ({"status": "ok"}, True),
        ({"status": "ok", "action": "set_current"}, True),
        ({"ok": True}, False),
        ({"success": "true"}, False),
        ({}, False),
    ],
)
def test_a_worker_save_is_acknowledged_by_either_success_key_and_by_nothing_else(
    tmp_path, monkeypatch, local_install, answer, accepted
):
    """97 of the 98 hub clones answer {"success": true}; wandb_mock answers {"status": "ok"}.

    wandb's /post is otherwise honest -- a failed fs.writeFileSync is caught and returned as a 400 --
    so only the spelling of success differs, and rewriting 35 lines of its preview server to rename
    the key is a patch we would have to keep re-anchoring. The check is widened instead.

    What must not widen with it is the floor: a bare 2xx is not an acknowledgement. This check is the
    one that catches an app serving nothing but HTTP 400, which two apps did for hours while the
    suite stayed green at 2,314 tests, so {} and a truthy-but-wrong key still have to fail.
    """
    real_update = hub_app.HubClient.update

    def update(self, sid, state):
        real_update(self, sid, state)  # the write still happens; only the acknowledgement is swapped
        return answer

    monkeypatch.setattr(hub_app.HubClient, "update", update)
    call = {
        "app": APP,
        "source": FIXTURE,
        "work": tmp_path,
        "state": copy.deepcopy(STATE),
        "schema_text": (FIXTURE / "SCHEMA.md").read_text(),
    }
    if accepted:
        assert hub_app.smoke(**call)["checks"]["worker_proxy_allows_app_saves"] is True
    else:
        with pytest.raises(ValueError, match="worker_proxy_allows_app_saves"):
            hub_app.smoke(**call)
