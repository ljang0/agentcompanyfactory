import io
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

from company_envs.world import vm as worker_vm
from company_envs.world.vm import SNAPSHOT_NAME, WorkerVM


@pytest.fixture
def vm(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_vm, "free_port", iter([12001, 12002]).__next__)
    monkeypatch.setattr(worker_vm.subprocess, "run", lambda *args, **kwargs: None)
    return WorkerVM(tmp_path, "worker", tmp_path / "base.qcow2", 8080)


class QMPStream(io.BytesIO):
    def __init__(self, replies):
        super().__init__(replies)
        self.requests = []

    def write(self, data):
        self.requests.append(json.loads(data))
        return len(data)


@pytest.fixture
def qmp(vm, monkeypatch):
    def connect(replies):
        stream = QMPStream(replies)
        seen = {}

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def settimeout(self, timeout):
                seen["timeout"] = timeout

            def connect(self, path):
                assert path == str(vm.qmp)

            def makefile(self, mode):
                assert mode == "rwb"
                return stream

        def socket_factory(family):
            assert family == socket.AF_UNIX
            return Socket()

        monkeypatch.setattr(worker_vm.socket, "socket", socket_factory)
        return stream, seen

    return connect


def test_save_snapshot_waits_for_completion_and_ignores_events(vm, qmp):
    stream, seen = qmp(b'{"QMP":{}}\n{"return":{}}\n{"event":"STOP"}\n{"event":"RESUME"}\n{"return":""}\n')
    vm.save_snapshot()
    assert stream.closed
    assert seen["timeout"] == 120
    assert stream.requests == [
        {"execute": "qmp_capabilities"},
        {"execute": "human-monitor-command", "arguments": {"command-line": f"savevm {SNAPSHOT_NAME}"}},
    ]


@pytest.mark.parametrize(
    "replies, message",
    [
        (b'{"QMP":{}}\n{"error":{"desc":"capabilities failed"}}\n', "capabilities failed"),
        (b'{"QMP":{}}\n{"return":{}}\n{"error":{"desc":"unsupported"}}\n', "unsupported"),
        (b'{"QMP":{}}\n{"return":{}}\n{"return":"Error: cannot save"}\n', "cannot save"),
        (b'{"QMP":{}}\n{"return":{}}\n', "closed before replying"),
        (b"", "closed before its greeting"),
    ],
)
def test_save_snapshot_reports_failures(vm, qmp, replies, message):
    stream, _ = qmp(replies)
    with pytest.raises(RuntimeError, match=message):
        vm.save_snapshot()
    assert stream.closed


def test_snapshot_timeout_closes_stream(vm, qmp, monkeypatch):
    stream, _ = qmp(b'{"QMP":{}}\n{"return":{}}\n')
    monkeypatch.setattr(stream, "readline", lambda: (_ for _ in ()).throw(TimeoutError("QMP timed out")))
    with pytest.raises(TimeoutError, match="QMP timed out"):
        vm.save_snapshot()
    assert stream.closed


def test_screenshot_keeps_qmp_contract(vm, qmp, tmp_path):
    stream, seen = qmp(b'{"QMP":{}}\n{"return":{}}\n{"event":"STOP"}\n{"return":{}}\n')
    target = tmp_path / "screen.png"
    vm.screenshot(target)
    assert seen["timeout"] == 10
    assert stream.requests[-1] == {
        "execute": "screendump",
        "arguments": {"filename": str(target), "format": "png"},
    }


@pytest.mark.parametrize("snapshot", [False, True])
def test_boot_selects_saved_state_and_waits_for_ssh(vm, monkeypatch, snapshot):
    commands = []
    process = SimpleNamespace(poll=lambda: None)

    def popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr(worker_vm.subprocess, "Popen", popen)
    monkeypatch.setattr(worker_vm.time, "sleep", lambda _: None)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 6)
        return SimpleNamespace(returncode=1 if len(calls) == 2 else 0)

    monkeypatch.setattr(vm, "run", run)
    original = vm.command.copy()
    assert vm.boot(snapshot=snapshot) is vm
    assert vm.process is process
    assert len(calls) == 3
    assert all(command == "true" for command, _ in calls)
    assert commands == [original + (["-loadvm", SNAPSHOT_NAME] if snapshot else [])]
    assert vm.command == original


def test_reconnect_keeps_owned_disk_ports_and_network(vm, monkeypatch):
    disk = vm.directory / "worker.qcow2"
    disk.write_text("saved overlay")
    inode = disk.stat().st_ino
    record = {"command": vm.command, "ssh_port": vm.ssh_port, "cdp_port": vm.cdp_port}

    def unexpected(*args, **kwargs):
        pytest.fail("Reconnecting must not create a new overlay or allocate new ports")

    monkeypatch.setattr(worker_vm.subprocess, "run", unexpected)
    monkeypatch.setattr(worker_vm, "free_port", unexpected)
    restored = WorkerVM.from_snapshot(vm.directory.parent, vm.worker, record)
    assert disk.stat().st_ino == inode
    assert restored.command == vm.command and restored.command is not vm.command
    assert restored.ssh_base == vm.ssh_base
    assert restored.env["SSHPASS"] == vm.env["SSHPASS"]
    assert restored.qmp == vm.qmp
    assert restored.process is None and restored.tunnel is None


def test_a_guest_gets_more_memory_than_a_five_megabyte_app_needs(tmp_path, monkeypatch):
    """At 4096 the guest, not the host, was the scarce machine.

    Measured in live guests: coinbase_mock's 5.8 MB state with four other company apps open exhausted
    the guest and the kernel killed Chrome, and adp_mock's 202,936-character page had its renderer
    OOM-killed, so the app could not be screenshotted at all. The host has 503 GB and the VM budget
    is 8 slots of 5 guests, so 40 guests cost 240 GB here against 160 GB before.
    """
    monkeypatch.setattr(worker_vm.subprocess, "run", lambda *args, **kwargs: None)
    machine = worker_vm.WorkerVM(tmp_path, "boss", tmp_path / "base.qcow2", app_port=8000)
    assert machine.memory_mb == worker_vm.GUEST_MEMORY_MB >= 6144
    assert machine.command[machine.command.index("-m") + 1] == str(worker_vm.GUEST_MEMORY_MB)
    smaller = worker_vm.WorkerVM(tmp_path, "analyst", tmp_path / "base.qcow2", app_port=8000, memory_mb=2048)
    assert smaller.command[smaller.command.index("-m") + 1] == "2048"
    with pytest.raises(ValueError, match="at least 1024"):
        worker_vm.WorkerVM(tmp_path, "clerk", tmp_path / "base.qcow2", app_port=8000, memory_mb=64)
