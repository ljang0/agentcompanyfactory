"""Capture and timing checks with fake SSH, desktop tools and CDP peers."""

import asyncio
import base64
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from company_envs.storage import write
from company_envs.world import screenshot as capture
from company_envs.world.harness import FakeBackend

PNG = FakeBackend.PNG


@pytest.mark.parametrize("mode", ["capture", "exit", "truncated", "invalid", "base64", "timeout", "cancel"])
def test_backend_transport_keeps_deadlines_and_checks_output(mode):
    class Transport:
        async def run(self, command, *, deadline, limit):
            assert command == capture.GUEST_CAPTURE and deadline == 123
            assert limit == 16 * 1024 * 1024
            if mode == "timeout":
                raise TimeoutError("deadline")
            if mode == "cancel":
                raise asyncio.CancelledError
            return {
                "stdout": "!" if mode == "base64" else base64.b64encode(b"bad" if mode == "invalid" else PNG),
                "stderr": base64.b64encode(b"capture failed"),
                "exit_code": int(mode == "exit"),
                "truncated": {"stdout": mode == "truncated"},
            }

    async def run():
        if mode == "capture":
            assert await capture.screenshot_transport(Transport(), deadline=123) == PNG
        else:
            error = {"timeout": TimeoutError, "cancel": asyncio.CancelledError}.get(mode, RuntimeError)
            with pytest.raises(error):
                await capture.screenshot_transport(Transport(), deadline=123)

    asyncio.run(run())


@pytest.fixture
def bench():
    path = Path(__file__).resolve().parents[1] / "scripts/screenshot_bench.py"
    spec = importlib.util.spec_from_file_location("screenshot_bench", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_desktop_uses_only_the_requested_vm(monkeypatch):
    vm = SimpleNamespace(ssh_base=["ssh", "-p", "23456", "ga@127.0.0.1"], env={"SSHPASS": "test"})
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, PNG, b"")

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture.screenshot(vm) == PNG
    assert calls == [
        (
            [*vm.ssh_base, capture.GUEST_CAPTURE],
            {"env": vm.env, "capture_output": True, "timeout": 30, "check": False},
        )
    ]


@pytest.mark.parametrize("method", ["screenshot", "cdp_screenshot"])
@pytest.mark.parametrize("failure", ["exit", "missing", "timeout", "invalid"])
def test_capture_failures_are_reported(monkeypatch, method, failure):
    vm = SimpleNamespace(ssh_base=["ssh", "guest"], env={}, cdp_port=23456)

    def run(argv, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("missing executable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 1 if failure == "exit" else 0, b"bad", b"tool failed")

    monkeypatch.setattr(capture.subprocess, "run", run)
    expected = TimeoutError if failure == "timeout" else RuntimeError
    with pytest.raises(expected, match="screenshot"):
        getattr(capture, method)(vm)


@pytest.mark.parametrize("png", [b"", b"not PNG", PNG[:40], PNG[:-12]])
def test_invalid_or_truncated_png_is_rejected(png):
    with pytest.raises(RuntimeError, match="complete PNG"):
        capture.validate_png(png)


def test_other_image_formats_are_rejected():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2)).save(stream, "JPEG")
    with pytest.raises(RuntimeError, match="complete PNG"):
        capture.validate_png(stream.getvalue())


@pytest.mark.parametrize("port", [None, True, 0, 65536, "9222"])
def test_cdp_rejects_invalid_ports_before_dispatch(monkeypatch, port):
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(ValueError, match="CDP port"):
        capture.cdp_screenshot(SimpleNamespace(cdp_port=port))


def test_cdp_command_uses_the_existing_tunnel(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["node", "--experimental-websocket", "-e", capture._CDP_CAPTURE, "23456"]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(argv, 0, PNG, b"")

    monkeypatch.setattr(capture.subprocess, "run", run)
    assert capture.cdp_screenshot(SimpleNamespace(cdp_port=23456)) == PNG


@pytest.mark.parametrize("mode", ["gnome", "fallback", "empty", "missing", "failed", "no_display"])
def test_guest_tools_and_cleanup_with_fake_executables(tmp_path, mode):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("mktemp", "rm", "cat", "timeout"):
        (bindir / name).symlink_to(shutil.which(name))
    source = tmp_path / "frame.png"
    source.write_bytes(PNG)
    log = tmp_path / "calls"
    socket_path = tmp_path / "X7"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(socket_path))
    try:
        for tool in ("gnome-screenshot", "import"):
            if mode == "missing":
                continue
            success = mode not in {"failed", "no_display"} and (tool == "import" or mode == "gnome")
            body = (
                "#!/bin/sh\n"
                'echo "$0 $DISPLAY $*" >> "$CAPTURE_LOG"\n'
                'test "$DBUS_SESSION_BUS_ADDRESS" = unix:path=/run/user/1000/bus || exit 9\n'
            )
            if success:
                body += 'for target do :; done\ncat "$PNG_SOURCE" > "$target"\n'
            elif mode == "empty" and tool == "gnome-screenshot":
                body += "exit 0\n"
            else:
                body += "exit 1\n"
            path = bindir / tool
            path.write_text(body)
            path.chmod(0o755)
        command = capture.GUEST_CAPTURE.replace(
            "/tmp/.X11-unix/X*", str(tmp_path / ("absent*" if mode == "no_display" else "X*"))
        )
        temporary = tmp_path / "captures"
        temporary.mkdir()
        result = subprocess.run(
            [shutil.which("bash"), "-c", command],
            env=os.environ
            | {
                "PATH": str(bindir),
                "TMPDIR": str(temporary),
                "PNG_SOURCE": str(source),
                "CAPTURE_LOG": str(log),
                "DISPLAY": ":stale",
            },
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert not list(temporary.iterdir())
        if mode in {"gnome", "fallback", "empty"}:
            assert result.returncode == 0 and result.stdout == PNG
            calls = log.read_text()
            assert ":7" in calls and ":stale" not in calls
            assert ("import" in calls) == (mode != "gnome")
        else:
            assert result.returncode != 0 and not result.stdout
            assert b"No X desktop" in result.stderr if mode == "no_display" else b"failed" in result.stderr
    finally:
        sock.close()


# Run only the embedded JavaScript, with every network operation replaced. No
# browser, npm package or service is needed to check the CDP protocol exchange.
@pytest.mark.parametrize(
    "mode", ["capture", "single", "unfocused", "empty", "protocol_error", "closed", "http_error", "bad_url"]
)
def test_cdp_protocol_with_fake_peer(mode):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the CDP helper")
    fake = r"""
const assert = require('node:assert/strict');
process.argv[1] = '23456';
let closed = 0;
global.fetch = async url => {
    assert.equal(url, 'http://127.0.0.1:23456/json/list');
    return {ok: MODE !== 'http_error', status: 503, json: async () => MODE === 'empty' ? [] : [
        {type: 'service_worker'},
        {type: 'page', webSocketDebuggerUrl: MODE === 'bad_url'
            ? 'ws://guest:9222/invalid' : 'ws://guest:9222/devtools/page/background'},
        {type: 'page', webSocketDebuggerUrl: 'ws://guest:9222/devtools/page/focused'}
    ].filter(page => MODE !== 'single' || page.webSocketDebuggerUrl?.endsWith('/focused'))};
};
global.WebSocket = class {
    constructor(url) {
        assert(url.startsWith('ws://127.0.0.1:23456/devtools/page/'));
        this.focused = url.endsWith('/focused');
        queueMicrotask(() => this.onopen());
    }
    close() { closed++; }
    send(text) {
        const message = JSON.parse(text);
        if (MODE === 'closed') return queueMicrotask(() => this.onclose());
        let result;
        if (message.method === 'Runtime.evaluate') {
            assert.equal(message.params.expression, 'document.hasFocus()');
            result = {result: {value: this.focused && !['unfocused', 'single'].includes(MODE)}};
        } else {
            assert(this.focused);
            assert.equal(message.method, 'Page.captureScreenshot');
            assert.deepEqual(message.params, {
                format: 'png', fromSurface: true, captureBeyondViewport: false
            });
            result = {data: PNG};
        }
        queueMicrotask(() => {
            this.onmessage({data: JSON.stringify({method: 'Page.event'})});
            this.onmessage({data: JSON.stringify(MODE === 'protocol_error'
                ? {id: message.id, error: {message: 'capture failed'}}
                : {id: message.id, result})});
        });
    }
};
process.on('exit', () => assert.equal(closed,
    ['empty', 'http_error', 'bad_url'].includes(MODE) ? 0
        : ['protocol_error', 'closed', 'single'].includes(MODE) ? 1 : 2));
"""
    script = f"const MODE = {json.dumps(mode)}; const PNG = {json.dumps(base64.b64encode(PNG).decode())};\n"
    result = subprocess.run(
        [node, "-e", script + fake + capture._CDP_CAPTURE], capture_output=True, timeout=5, check=False
    )
    if mode in {"capture", "single"}:
        assert result.returncode == 0, result.stderr
        assert result.stdout == PNG
    else:
        assert result.returncode == 1 and not result.stdout
        assert b"AssertionError" not in result.stderr


@pytest.mark.parametrize("method", ["guest", "cdp"])
def test_twenty_frame_timings_and_first_frame(bench, monkeypatch, method):
    vm = object()
    frames = []
    ticks = iter(
        value for i in range(20) for value in (i * 100_000_000, i * 100_000_000 + (i + 1) * 1_000_000)
    )

    def shot(target):
        assert target is vm
        frames.append(len(frames))
        return PNG + bytes([frames[-1]])

    monkeypatch.setattr(bench, "screenshot" if method == "guest" else "cdp_screenshot", shot)
    monkeypatch.setattr(bench.time, "perf_counter_ns", lambda: next(ticks))
    report, first = bench.benchmark(vm, method)
    assert len(frames) == 20 and first == PNG + b"\0"
    assert report["samples_ms"] == list(range(1, 21))
    assert report["median_ms"] == 10.5 and report["p95_ms"] == 19
    assert report["frames"] == 20 and report["warmup"] == 0


def test_benchmark_does_not_hide_failed_captures(bench, monkeypatch):
    def fail(vm):
        raise RuntimeError("capture failed")

    monkeypatch.setattr(bench, "screenshot", fail)
    with pytest.raises(RuntimeError, match="capture failed"):
        bench.benchmark(object())


def vm_record(tmp_path):
    path = tmp_path / "worker/vm.json"
    record = {
        "worker": "worker",
        "status": "running",
        "dry_run": False,
        "vm": {"ssh_port": 23456, "cdp_port": 34567, "process": {"pid": 123, "start_ticks": "1"}},
    }
    write(path, record)
    return path, record


def test_benchmark_cli_attaches_and_saves(bench, monkeypatch, tmp_path, capsys):
    path, record = vm_record(tmp_path)
    monkeypatch.setattr(bench, "_process_identity", lambda pid: record["vm"]["process"])

    def measure(vm, method):
        assert "23456" in vm.ssh_base and vm.ssh_base[-1] == "ga@127.0.0.1"
        assert vm.cdp_port == 34567 and method == "cdp"
        assert vm.env["SSHPASS"] == "password123"
        return {"frames": 20, "median_ms": 12, "p95_ms": 18}, PNG

    monkeypatch.setattr(bench, "benchmark", measure)
    image, report = tmp_path / "out/screen.png", tmp_path / "out/bench.json"
    assert (
        bench.main([str(path), "--method", "cdp", "--save-first", str(image), "--output-json", str(report)])
        == 0
    )
    assert image.read_bytes() == PNG
    assert json.loads(capsys.readouterr().out) == json.loads(report.read_text())


@pytest.mark.parametrize("problem", ["dry_run", "stopped", "worker", "stale", "missing_identity"])
def test_benchmark_refuses_wrong_or_stale_records(bench, monkeypatch, tmp_path, problem):
    path, record = vm_record(tmp_path)
    if problem == "dry_run":
        record["dry_run"] = True
    elif problem == "stopped":
        record["status"] = "stopped"
    elif problem == "worker":
        record["worker"] = "other"
    elif problem == "missing_identity":
        record["vm"].pop("process")
    write(path, record)
    monkeypatch.setattr(bench, "_process_identity", lambda pid: None)
    monkeypatch.setattr(bench, "benchmark", lambda *a: pytest.fail("must not capture"))
    with pytest.raises(ValueError):
        bench.main([str(path)])
