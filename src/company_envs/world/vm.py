"""Owned QEMU overlays with a single explicitly forwarded app service.

No host mounts, host networking, inherited running VM or broad process cleanup.
This launcher is Linux/KVM-specific; it is not a portable cloud provisioner.
"""

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import time
from pathlib import Path

SNAPSHOT_NAME = "episode-start"
# Guest RAM. Measured in live guests on 2026-09-09/10 at the old 4096: coinbase_mock's 5.8 MB state
# with four other company apps open exhausted the guest and the kernel killed Chrome, and adp_mock's
# 202,936-character page had its renderer OOM-killed so the app could not be screenshotted at all.
# The host this runs on has 503 GB, and the VM budget is 8 slots of 5 guests, so 40 guests cost
# 240 GB here against 160 GB before -- the guest was the scarce machine, not the host.
GUEST_MEMORY_MB = 6144


def qmp_socket_path(directory):
    """A short, unique socket path: unix sockets are limited to 107 bytes and company paths are long."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "company-envs-vm"
    base.mkdir(parents=True, exist_ok=True)
    return base / (hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()[:16] + ".qmp")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WorkerVM:
    def __init__(self, directory, worker, base_image, app_port, clock=None, memory_mb=None):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", worker):
            raise ValueError("Invalid worker identifier")
        self.directory = Path(directory).resolve() / worker
        self.directory.mkdir(parents=True, exist_ok=False)
        self.worker = worker
        self.ssh_port, self.cdp_port = free_port(), free_port()
        self._configure_connection()
        disk = self.directory / "worker.qcow2"
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(Path(base_image).resolve()),
                str(disk),
            ],
            check=True,
            capture_output=True,
        )
        self.memory_mb = int(memory_mb or os.environ.get("COMPANY_ENVS_VM_MEMORY_MB") or GUEST_MEMORY_MB)
        if self.memory_mb < 1024:
            raise ValueError("A guest desktop needs at least 1024 MB")
        self.command = [
            "qemu-system-x86_64",
            "-enable-kvm",
            "-cpu",
            "host",
            "-m",
            str(self.memory_mb),
            "-smp",
            "2",
            "-drive",
            f"file={disk},if=virtio,format=qcow2",
            "-device",
            "virtio-vga",
            "-device",
            "qemu-xhci",
            "-device",
            "usb-tablet",
            "-display",
            "none",
            "-qmp",
            f"unix:{self.qmp},server=on,wait=off",
            "-serial",
            "file:" + str(self.directory / "serial.log"),
            "-netdev",
            f"user,id=n0,restrict=on,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22,guestfwd=tcp:10.0.2.100:80-cmd:nc 127.0.0.1 {int(app_port)}",
            "-device",
            "virtio-net-pci,netdev=n0",
        ]

        if clock:
            # The world is a snapshot at its reference instant; the guest wakes up at that instant
            # whatever the real date, so a task reads the same on any day it is run. Network is
            # restricted, so nothing inside can re-sync the clock.
            self.command += ["-rtc", f"base={clock},clock=vm"]
        self.clock = clock

    def _configure_connection(self):
        self.qmp = qmp_socket_path(self.directory)
        self.process = self.tunnel = None
        self.env = os.environ | {"SSHPASS": "password123"}  # Public credential of the local base image.
        self.ssh_base = [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.directory / 'known_hosts'}",
            "-o",
            "ConnectTimeout=3",
            "-p",
            str(self.ssh_port),
            "ga@127.0.0.1",
        ]

    @classmethod
    def from_snapshot(cls, directory, worker, record):
        """Reconnect to an owned overlay after its recorded processes have stopped."""
        vm = cls.__new__(cls)
        vm.directory = Path(directory).resolve() / worker
        vm.worker = worker
        vm.ssh_port, vm.cdp_port = record["ssh_port"], record["cdp_port"]
        vm.command = list(record["command"])
        vm._configure_connection()
        return vm

    def boot(self, *, snapshot=False):
        with (self.directory / "qemu.log").open("w") as log:
            command = [*self.command, "-loadvm", SNAPSHOT_NAME] if snapshot else self.command
            self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.worker} QEMU exited: {self.directory / 'qemu.log'}")
            try:
                if self.run("true", timeout=6, check=False).returncode == 0:
                    return self
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.5)
        raise TimeoutError(f"{self.worker} SSH did not become ready")

    def run(self, command, timeout=30, check=True, input=None):
        return subprocess.run(
            [*self.ssh_base, command],
            env=self.env,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=check,
            input=input,
        )

    def upload(self, source, destination):
        if not Path(destination).is_relative_to("/home/ga") or ".." in Path(destination).parts:
            raise ValueError("Uploads are restricted to the owned guest home")
        subprocess.run(
            [
                "sshpass",
                "-e",
                "scp",
                "-q",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={self.directory / 'known_hosts'}",
                "-P",
                str(self.ssh_port),
                str(source),
                "ga@127.0.0.1:" + shlex.quote(destination),
            ],
            env=self.env,
            capture_output=True,
            check=True,
            timeout=120,
        )

    def open_browser(self):
        result = self.run(
            "if test -x /home/ga/browser/chrome; then echo /home/ga/browser/chrome; "
            "else command -v google-chrome || command -v chromium || command -v chromium-browser; fi",
            check=False,
        )
        executable = result.stdout.strip().splitlines()
        if not executable:
            raise RuntimeError("No Chromium browser in base image; no package installation attempted")
        # This is an actual visible browser inside the guest's desktop session.
        displays = self.run("ls /tmp/.X11-unix").stdout.split()
        display = next((":" + d[1:] for d in displays if re.fullmatch(r"X\d+", d)), None)
        if display is None:
            raise RuntimeError("No X desktop in base image")
        # Initialize only a fresh owned profile. Do not overwrite a running or
        # preexisting profile to suppress a prompt. Chromium's credential-saving
        # preference is documented in components/password_manager/core/common/
        # password_manager_pref_names.h (kCredentialsEnableService).
        self.run("mkdir /home/ga/worker-browser && mkdir /home/ga/worker-browser/Default")
        self.upload(
            Path(__file__).with_name("browser_preferences.json"),
            "/home/ga/worker-browser/Default/Preferences",
        )
        self.run(
            f"DISPLAY={display} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus nohup {shlex.quote(executable[0])} "
            "--no-first-run --no-default-browser-check --remote-debugging-port=9222 "
            # Disposable synthetic profiles must not block on GNOME's interactive
            # first-use keyring dialog. No real user passwords enter this VM.
            "--password-store=basic "
            "--user-data-dir=/home/ga/worker-browser http://10.0.2.100/login "
            ">/home/ga/worker-browser.log 2>&1 </dev/null &",
            check=False,
        )
        self.tunnel = subprocess.Popen(
            [
                *self.ssh_base[:-1],
                "-o",
                "ExitOnForwardFailure=yes",
                "-N",
                "-L",
                f"127.0.0.1:{self.cdp_port}:127.0.0.1:9222",
                self.ssh_base[-1],
            ],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self.cdp_port

    def qmp_command(self, command, arguments=None, *, timeout=10):
        """Wait for a QMP reply, ignoring asynchronous VM events."""
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.qmp))
            with sock.makefile("rwb") as stream:
                if not stream.readline():
                    raise RuntimeError("QMP closed before its greeting")
                for request in [
                    {"execute": "qmp_capabilities"},
                    {"execute": command, "arguments": arguments or {}},
                ]:
                    stream.write(json.dumps(request).encode() + b"\n")
                    stream.flush()
                    while True:
                        line = stream.readline()
                        if not line:
                            raise RuntimeError("QMP closed before replying")
                        response = json.loads(line)
                        if "error" in response:
                            raise RuntimeError(str(response["error"]))
                        if "return" in response:
                            break
                return response["return"]

    def save_snapshot(self):
        """Save the ready desktop, memory and disk in this worker's overlay."""
        result = self.qmp_command(
            "human-monitor-command", {"command-line": f"savevm {SNAPSHOT_NAME}"}, timeout=120
        )
        # The human monitor reports command failures as text inside a successful QMP reply.
        if result.strip():
            raise RuntimeError(f"Could not save guest snapshot: {result.strip()}")

    def screenshot(self, target):
        self.qmp_command("screendump", {"filename": str(Path(target).resolve()), "format": "png"})

    def close(self):
        for process in (self.tunnel, self.process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
