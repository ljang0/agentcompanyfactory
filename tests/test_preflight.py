"""Capability diagnostics and guards stay offline and preserve work on setup failure."""

import errno
import json
import sys
import tomllib

import pytest
from pydantic import BaseModel

from company_envs import __main__ as cli
from company_envs import models, preflight
from company_envs.storage import read, write
from company_envs.world import hub_world


class Answer(BaseModel):
    value: str


def deny_sockets(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(preflight.socket, "socket", denied)


def test_probe_reports_actual_socket_denial_without_guessing_its_cause(monkeypatch):
    deny_sockets(monkeypatch)
    report = preflight.inspect_environment({}, scope="runtime")
    assert report["status"] == "environment_blocked"
    assert report["model_calls"] == 0
    assert report["checks"][0]["errno"] == errno.EPERM
    assert "create IPv4" in report["checks"][0]["detail"]
    with pytest.raises(preflight.EnvironmentBlocked, match="enclosing sandbox"):
        preflight.require_runtime_environment()


def test_loopback_probe_closes_sockets_on_success_and_failure(monkeypatch):
    instances = []
    fail_connect = False

    class Socket:
        def __init__(self, *args):
            self.closed = False
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def settimeout(self, seconds):
            assert seconds == 1

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def listen(self, backlog):
            assert backlog == 1

        def getsockname(self):
            return ("127.0.0.1", 12345)

        def connect(self, address):
            if fail_connect:
                raise PermissionError(errno.EACCES, "connect denied")

        def accept(self):
            return Socket(), ("127.0.0.1", 12346)

        def sendall(self, data):
            assert data == b"p"

        def recv(self, length):
            return b"p"

    monkeypatch.setattr(preflight.socket, "socket", Socket)
    assert preflight.inspect_environment({}, scope="runtime")["ok"]
    assert len(instances) == 3 and all(s.closed for s in instances)
    instances.clear()
    fail_connect = True
    result = preflight.inspect_environment({}, scope="runtime")
    assert not result["ok"] and "connect and exchange" in result["checks"][0]["detail"]
    assert len(instances) == 2 and all(s.closed for s in instances)


def test_profile_probe_cleans_up_and_does_not_touch_login(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "_tcp", lambda **kw: {"status": "pass", "detail": "double"})
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/codex")
    secret = tmp_path / "auth.json"
    secret.write_text("do not read or change this secret")
    config = {"models": {"codex_homes": [str(tmp_path)]}}
    report = preflight.inspect_environment(config, scope="models")
    assert report["ok"]
    assert list(tmp_path.iterdir()) == [secret]
    assert secret.read_text() == "do not read or change this secret"
    assert "secret" not in json.dumps(report)

    def readonly(**kwargs):
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(preflight.tempfile, "TemporaryFile", readonly)
    report = preflight.inspect_environment(config, scope="models")
    assert not report["ok"]
    assert report["checks"][-1]["errno"] == errno.EROFS
    assert report["checks"][-1]["check"] == f"codex_profile_write:{tmp_path}"


def test_permissions_are_generated_from_config_without_writing_it(tmp_path, monkeypatch):
    homes = [str(tmp_path / 'account "one"'), str(tmp_path / "other account")]
    config = {"models": {"codex_homes": homes}}
    generated = preflight.permissions_config(config)
    profile = tomllib.loads(generated)["permissions"]["company-pipeline"]
    assert profile["filesystem"] == dict.fromkeys(homes, "write")
    assert profile["network"]["enabled"] is True
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv("CODEX_HOME", homes[1])
    assert preflight.configured_homes({}) == [homes[1]]


def test_blocked_model_dispatch_spends_no_attempts_and_can_resume(tmp_path, monkeypatch):
    deny_sockets(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(models, "require_model_environment", preflight.require_model_environment)
    config = {
        "generation": {"completion_timeout_seconds": 1},
        "models": {"expand": ["codex/gpt-test", "codex/gpt-other"], "reasoning": "high"},
    }
    backend = models.Models(config, tmp_path, max_calls=1, cumulative=True)
    with pytest.raises(models.ModelUnavailable, match="environment blocked") as error:
        backend.call("expand", "hello", Answer)
    assert models.failure_policy(error.value) == (False, True)
    assert backend.calls_spent == models.dispatched_calls(tmp_path) == 0
    reports = list(tmp_path.glob("calls/*/preflight/*.json"))
    assert len(reports) == 1 and read(reports[0])["status"] == "environment_blocked"

    def execute(cmd, prompt, directory, timeout):
        write(directory / "answer.json", {"value": "saved"})
        (directory / "stdout.jsonl").write_text("")
        return 0, 0.01

    monkeypatch.setattr(models, "require_model_environment", lambda home: None)
    monkeypatch.setattr(models, "execute", execute)
    assert backend.call("expand", "hello", Answer)[0].value == "saved"
    assert models.dispatched_calls(tmp_path) == 1
    # Restoring the blocked environment cannot prevent reuse of a valid cached answer.
    monkeypatch.setattr(models, "require_model_environment", preflight.require_model_environment)
    assert backend.call("expand", "hello", Answer)[0].value == "saved"
    assert models.dispatched_calls(tmp_path) == 1


def test_hub_preflight_preserves_handoff_and_attribution_on_failure(tmp_path, monkeypatch):
    deny_sockets(monkeypatch)
    world = hub_world.CompanyWorld.__new__(hub_world.CompanyWorld)
    world.folder, world.log_dir = tmp_path, tmp_path / "logs"
    write(tmp_path / "runtime/endpoints.json", {"apps": "previous"})
    write(tmp_path / "runtime/attribution/app.jsonl", {"worker_id": "w1"})
    with pytest.raises(preflight.EnvironmentBlocked):
        world.start()
    assert read(tmp_path / "runtime/endpoints.json") == {"apps": "previous"}
    assert read(tmp_path / "runtime/attribution/app.jsonl") == {"worker_id": "w1"}
    assert not world.log_dir.exists()
    assert len(list(tmp_path.glob("runtime/preflight/*.json"))) == 1


def test_preflight_cli_uses_selected_config_and_reports_nonzero(tmp_path, monkeypatch, capsys):
    deny_sockets(monkeypatch)
    config = tmp_path / "selected.toml"
    config.write_text('[models]\ncodex_homes = ["accounts/selected"]\n')
    report = tmp_path / "report.json"
    args = ["company-envs", "--root", str(tmp_path), "--config", str(config)]
    monkeypatch.setattr(sys, "argv", [*args, "preflight", "--output", str(report)])
    with pytest.raises(SystemExit) as error:
        cli.main()
    result = json.loads(capsys.readouterr().out)
    assert error.value.code == 1 and result == read(report)
    assert any(str(tmp_path / "accounts/selected") in row["check"] for row in result["checks"])
    monkeypatch.setattr(sys, "argv", [*args, "permissions-config"])
    cli.main()
    generated = tomllib.loads(capsys.readouterr().out)
    assert generated["permissions"]["company-pipeline"]["filesystem"] == {
        str(tmp_path / "accounts/selected"): "write"
    }


def test_profile_initialization_errors_do_not_retry():
    error = models.provider_failure("turn/start failed: Read-only file system (os error 30)")
    assert models.failure_policy(error) == (False, True)
    assert not error.busy
