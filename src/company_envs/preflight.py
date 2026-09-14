"""Small capability probes; no model requests, app builds, or permission changes."""

import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

from .storage import now

REMEDY = (
    "Select host/session permissions that allow command networking and writes to the configured "
    "Codex profiles, then rerun preflight and the blocked stage. "
    "See docs/SANDBOX-TROUBLESHOOTING.md; repository code cannot change the enclosing sandbox."
)


class EnvironmentBlocked(RuntimeError):
    def __init__(self, report):
        self.report = report
        failures = "; ".join(
            f"{row['check']}: {row['detail']}" for row in report["checks"] if row["status"] == "fail"
        )
        super().__init__(f"Execution environment blocked: {failures}. {REMEDY}")


def configured_homes(config):
    homes = config.get("models", {}).get("codex_homes") or [
        os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    ]
    return list(dict.fromkeys(str(Path(home).expanduser().resolve()) for home in homes))


def _tcp(*, loopback):
    operation = "create IPv4 TCP socket"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.settimeout(1)
            if loopback:
                operation = "bind and listen on 127.0.0.1 at an ephemeral port"
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                operation = "connect and exchange bytes over loopback"
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(server.getsockname())
                    peer, _ = server.accept()
                    with peer:
                        peer.settimeout(1)
                        client.sendall(b"p")
                        if peer.recv(1) != b"p":
                            raise OSError("loopback probe did not receive its byte")
                        peer.sendall(b"p")
                        if client.recv(1) != b"p":
                            raise OSError("loopback probe did not receive its reply")
    except OSError as exc:
        return {"status": "fail", "detail": f"{operation}: {exc}", "errno": exc.errno}
    return {"status": "pass", "detail": operation}


def _profile_write(home):
    try:
        # A disposable file measures real mount/sandbox permissions; os.access does not.
        # No login contents are read, directories created, or existing files touched.
        with tempfile.TemporaryFile(prefix=".company-envs-preflight-", dir=home) as handle:
            handle.write(b"preflight")
            handle.flush()
    except OSError as exc:
        return {"status": "fail", "detail": f"temporary write in {home}: {exc}", "errno": exc.errno}
    return {"status": "pass", "detail": f"temporary file write and cleanup in {home}"}


def inspect_environment(config, *, scope="all"):
    if scope not in {"models", "runtime", "all"}:
        raise ValueError(f"unknown preflight scope: {scope}")
    checks = [
        {"check": "loopback_tcp" if scope != "models" else "tcp_socket", **_tcp(loopback=scope != "models")}
    ]
    if scope in {"models", "all"}:
        executable = shutil.which("codex")
        checks.append(
            {
                "check": "codex_executable",
                "status": "pass" if executable else "fail",
                "detail": executable or "Install the Codex CLI and make it available on PATH.",
            }
        )
        for home in configured_homes(config):
            checks.append({"check": f"codex_profile_write:{home}", **_profile_write(home)})
    ok = all(row["status"] == "pass" for row in checks)
    return {
        "schema_version": 1,
        "at": now(),
        "scope": scope,
        "ok": ok,
        "status": "capabilities_available" if ok else "environment_blocked",
        "model_calls": 0,
        "checks": checks,
        "unmeasured": [
            "Remote model authentication, quota and connectivity",
            "Existing profile database/subdirectory writes",
            "App builds, browser and VM execution, worker trials",
        ],
        "next_step": "Run the required stage; this is not acceptance proof." if ok else REMEDY,
    }


def require_model_environment(home=None):
    config = {"models": {"codex_homes": [home]}} if home else {}
    report = inspect_environment(config, scope="models")
    if not report["ok"]:
        raise EnvironmentBlocked(report)
    return report


def require_runtime_environment():
    report = inspect_environment({}, scope="runtime")
    if not report["ok"]:
        raise EnvironmentBlocked(report)
    return report


def permissions_config(config):
    """Print host settings for review/application outside this process, never apply them."""
    lines = [
        "# Host Codex settings, not a company-envs --config file.",
        "# Merge using the host's normal settings mechanism; managed policy still applies.",
        'default_permissions = "company-pipeline"',
        "",
        "[permissions.company-pipeline]",
        'extends = ":workspace"',
        "",
        "[permissions.company-pipeline.filesystem]",
        *(f'{json.dumps(home)} = "write"' for home in configured_homes(config)),
        "",
        "[permissions.company-pipeline.network]",
        "enabled = true",
        "",
        "# These local targets must also be allowed when a network proxy is active.",
        "[permissions.company-pipeline.network.domains]",
        '"localhost" = "allow"',
        '"127.0.0.1" = "allow"',
        "# Remote model domains follow the host's existing proxy policy.",
    ]
    return "\n".join(lines) + "\n"
