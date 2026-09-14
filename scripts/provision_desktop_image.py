#!/usr/bin/env python3
"""Build the desktop base image CUA-Gym's applications run in, and prove each one opens clean.

The pipeline boots worker guests from a qcow2 base (``world/vm.py``). That base carries a GNOME
desktop and evince and nothing else, so every CUA-Gym task typed ``libreoffice_calc``, ``vscode``,
``gimp``, ``vlc`` or ``blender`` has no application to open. This script installs them once, into a
new image, and suppresses the first-run state each one would otherwise put on top of the company's
data in every screenshot a worker and a judge ever see.

Two things about this guest are not what CUA-Gym's skills assume, and both waste a day if missed:

* **The X display is ``:1``.** Every skill says ``DISPLAY=:0``; there is no ``:0`` here, and a GUI
  launched against it fails silently with no window and no error.
* **The pipeline's own guests run QEMU ``restrict=on``**, so no provisioning can happen inside an
  episode. Installation has to be a separate, unrestricted boot -- which is what ``build`` is.

``build`` never writes to the source image: it opens a qcow2 overlay, boots that, and flattens the
result into a new file with ``qemu-img convert``. The source stays a backing file for live runs.

``verify`` boots the built image the way the pipeline does -- ``restrict=on``, no network -- seeds
real company materials through the repo's own ``world/documents.py`` renderer, opens each
application on them, screenshots, then kills the application uncleanly and opens it again. The
second launch is the one that matters: the controller stops episode VMs, so an application that
keeps recovery or session state greets the next task with a dialog instead of its work.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

GUEST_USER = "ga"
GUEST_PASSWORD = "password123"  # Public credential of the local base image.
DISPLAY = ":1"  # Not :0. See the module docstring.

DEFAULT_SOURCE = Path.home() / ".cache/gym-anything/qemu/base_ubuntu_gnome.qcow2"
DEFAULT_OUTPUT = Path("/mnt/storage/desktop-images/base_ubuntu_desktop.qcow2")
DEFAULT_WORK = Path("/mnt/storage/desktop-images/work")

# Everything stays on /mnt/storage: / has broken this session twice by filling.
BOOT_TIMEOUT = 900
MEMORY_MB = 6144

# Debian packages, and the CUA-Gym app types each one answers. Chrome is absent on purpose: the
# pipeline uploads Chrome for Testing per worker in ``world/hub_vm.py``, so baking a second browser
# would cost 400 MB to shadow the one the worker actually gets.
APT_PACKAGES = [
    ("libreoffice", "libreoffice_calc, libreoffice_writer, libreoffice_impress"),
    ("vlc", "vlc"),
    ("gimp", "gimp"),
    ("blender", "blender"),
    ("openshot-qt", "openshot (CUA-Gym skill, outside the taxonomy list)"),
    ("evince", "pdf (already present in the base; named so the manifest records its version)"),
    # The tools the proofs and any later in-guest check need: a screenshot, a window list, media.
    ("imagemagick", "in-guest screenshots for the app proofs"),
    ("wmctrl", "window list, to name what is actually on screen"),
    ("xdotool", "window control for later interaction checks"),
    ("ffmpeg", "media seeding for vlc"),
]
# VS Code is not in Ubuntu's archive; Microsoft's repo is the supported route and lets the manifest
# record an exact version.
VSCODE_REPO = (
    "deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] "
    "https://packages.microsoft.com/repos/code stable main"
)

VERSION_PROBES = {
    "libreoffice": "libreoffice --version | head -1",
    "vlc": "vlc --version 2>/dev/null | head -1",
    "gimp": "gimp --version",
    "blender": "blender --version 2>/dev/null | grep -i '^Blender' | head -1",
    "openshot-qt": "openshot-qt --version 2>&1 | grep -i 'OpenShot version' | head -1",
    "code": "code --version | head -1",
    "evince": "evince --version",
    "os": "lsb_release -ds",
}


# --------------------------------------------------------------------------------------------
# First-run suppression. Every entry below was found the same way: launch the application on a
# seeded company file under DISPLAY=:1, screenshot, look at what is covering the data.
# --------------------------------------------------------------------------------------------

LIBREOFFICE_SUPPRESS = r"""
import glob, os, re, shutil, subprocess, sys

PROFILE = os.path.expanduser("~/.config/libreoffice/4/user")
REG = os.path.join(PROFILE, "registrymodifications.xcu")

# The profile only exists after a first start. --terminate_after_init makes one with no window.
if not os.path.isfile(REG):
    subprocess.run(["libreoffice", "--headless", "--norestore", "--terminate_after_init"],
                   capture_output=True, timeout=240)
if not os.path.isfile(REG):
    sys.exit("no LibreOffice user profile at " + REG)

version = ""
out = subprocess.run(["libreoffice", "--version"], capture_output=True, text=True).stdout
match = re.search(r"LibreOffice (\d+\.\d+)", out)
if match:
    version = match.group(1)

text = open(REG).read()
before = len(text)

# A stored recovery list is what raises the Document Recovery modal after an unclean stop.
text = re.sub(r'<item oor:path="/org\.openoffice\.Office\.Recovery[^>]*>.*?</item>\s*', "",
              text, flags=re.S)

# AutoCorrect rewrites what a worker types, so a grader checking the text it asked for fails on a
# change the application made, not the worker. Typing
# `desktop probe 1789075589 teh 1st -- "q" TWo` into Writer on the unfixed image saved
# `Desktop probe 1789075589 the 1st - "q" Two` -- five rewrites from four different options. The
# whole family goes, not just the capitalisation that was first noticed: every one of these edits
# typed input, and the text operators are case- and character-sensitive by contract.
AUTOCORRECT = [
    "CapitalAtStartSentence",     # desktop -> Desktop
    "UseReplacementTable",        # teh -> the
    "ChangeDash",                 # -- -> en dash
    "ReplaceDoubleQuote",         # "q" -> curly quotes
    "ReplaceSingleQuote",         # 'q' -> curly quotes
    "TwoCapitalsAtStart",         # TWo -> Two
    "ChangeOrdinalNumber",        # 1st -> superscripted
    "RemoveDoubleSpaces",
    "SetInetAttribute",           # a typed URL becomes a hyperlink element in the saved XML
    "ChangeUnderlineWeight",
    "AddNonBreakingSpace",
    "TransliterateRTL",
    "ChangeAngleQuotes",
    "CorrectAccidentalCapsLock",
]

ENTRIES = [
    ("/org.openoffice.Office.Common/Misc", "ShowTipOfTheDay", "false"),
    ("/org.openoffice.Setup/Office", "FirstRun", "false"),
    ("/org.openoffice.Setup/Product", "ooSetupLastVersion", version),
    ("/org.openoffice.Office.Recovery/RecoveryInfo", "Enabled", "false"),
    ("/org.openoffice.Office.Recovery/AutoSave", "Enabled", "false"),
    # Every seeded material is a Microsoft format, so Ctrl+S raises a modal "Confirm File Format"
    # over the document on every single save until this is off.
    ("/org.openoffice.Office.Common/Save/Document", "WarnAlienFormat", "false"),
    # Calc completes a typed cell from the rest of the column, which rewrites input the same way.
    ("/org.openoffice.Office.Calc/Input", "AutoInput", "false"),
    ("/org.openoffice.Office.Writer/AutoFunction/Format/ByInput", "Enable", "false"),
    ("/org.openoffice.Office.Writer/AutoFunction/Completion", "Enable", "false"),
] + [("/org.openoffice.Office.Common/AutoCorrect", prop, "false") for prop in AUTOCORRECT]
items = ""
for path, prop, value in ENTRIES:
    text = re.sub(r'<item oor:path="%s"><prop oor:name="%s".*?</item>\s*'
                  % (re.escape(path), re.escape(prop)), "", text, flags=re.S)
    items += ('<item oor:path="%s"><prop oor:name="%s" oor:op="fuse"><value>%s</value></prop></item>\n'
              % (path, prop, value))
text = text.replace("</oor:items>", items + "</oor:items>")
open(REG, "w").write(text)

# Backups are the other half of recovery: a stored copy re-offers the document.
for path in glob.glob(os.path.join(PROFILE, "backup", "*")):
    shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)

print("registry %d -> %d bytes, %d entries, ooSetupLastVersion=%s"
      % (before, len(text), len(ENTRIES), version))
"""

# VS Code 1.137 opens a full-screen "Welcome to VS Code / Sign in to use GitHub Copilot" modal over
# the workspace, plus a Welcome tab, a workspace-trust prompt and a telemetry notice.
VSCODE_SETTINGS = {
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.welcomePage.walkthroughs.openOnInstall": False,
    "workbench.enableExperiments": False,
    "workbench.settings.enableNaturalLanguageSearch": False,
    "workbench.editor.untitled.hint": "hidden",
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "telemetry.telemetryLevel": "off",
    "telemetry.enableTelemetry": False,
    "telemetry.enableCrashReporter": False,
    "security.workspace.trust.enabled": False,
    "security.workspace.trust.startupPrompt": "never",
    "security.workspace.trust.banner": "never",
    "update.mode": "none",
    "update.showReleaseNotes": False,
    "extensions.autoUpdate": False,
    "extensions.autoCheckUpdates": False,
    "extensions.ignoreRecommendations": True,
    "chat.disableAIFeatures": True,
    "chat.commandCenter.enabled": False,
    "window.commandCenter": False,
    "git.openRepositoryInParentFolders": "never",
    "git.autofetch": False,
    "npm.fetchOnlinePackageInfo": False,
    "explorer.confirmDelete": False,
    "explorer.confirmDragAndDrop": False,
    # Hot exit restores unsaved buffers after the controller stops the VM, so the next task's
    # first screenshot is the previous task's editor.
    "files.hotExit": "off",
    "window.restoreWindows": "none",
}

# VLC raises a modal "Privacy and Network Access Policy" dialog dead centre over the video on first
# run, and its default is to allow metadata lookups a restricted guest can never complete.
VLCRC = """[core]
metadata-network-access=0
[qt]
qt-privacy-ask=0
qt-updates-notif=0
qt-recentplay=0
qt-notification=0
qt-system-tray=0
"""

# OpenShot opens a "Welcome!/Tutorial" dialog over the video preview and asks to send metrics.
# Its settings file is a JSON *array* of setting objects keyed by "setting", not a flat mapping,
# and it only exists after a first GUI start -- so the file is materialised, then patched in place.
OPENSHOT_SUPPRESS = r"""
import json, os, subprocess, time

SETTINGS = os.path.expanduser("~/.openshot_qt/openshot.settings")
if not os.path.isfile(SETTINGS):
    env = dict(os.environ, DISPLAY="__DISPLAY__")
    proc = subprocess.Popen(["openshot-qt"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(2)
        if os.path.isfile(SETTINGS):
            break
    proc.terminate()
    # -f "openshot" would match this script's own argv and kill the patcher mid-write.
    subprocess.run(["pkill", "-9", "-f", "bin/openshot-qt"], capture_output=True)
    time.sleep(3)
if not os.path.isfile(SETTINGS):
    raise SystemExit("openshot never wrote " + SETTINGS)

WANT = {"tutorial_enabled": False, "send_metrics": False,
        "tutorial_ids": "0,1,2,3,4,5,6,7,8,9,10"}
entries = json.load(open(SETTINGS))
changed = []
for entry in entries:
    name = entry.get("setting")
    if name in WANT and entry.get("value") != WANT[name]:
        entry["value"] = WANT[name]
        changed.append(name)
json.dump(entries, open(SETTINGS, "w"), indent=1)

# A recovery copy is OpenShot's version of the Document Recovery modal.
recovery = os.path.expanduser("~/.openshot_qt/recovery")
removed = 0
for name in os.listdir(recovery) if os.path.isdir(recovery) else []:
    os.remove(os.path.join(recovery, name))
    removed += 1
print(f"openshot settings patched: {changed or 'already set'}; {removed} recovery copies removed")
""".replace("__DISPLAY__", DISPLAY)

# Blender shows its splash over an empty scene whenever it is started without a file. Opening a
# .blend suppresses it, but a task that launches bare Blender would screenshot the splash.
BLENDER_SUPPRESS = (
    "blender --background --factory-startup --python-expr "
    "'import bpy; bpy.context.preferences.view.show_splash=False; "
    "bpy.context.preferences.view.show_tooltips_python=False; bpy.ops.wm.save_userpref()'"
)

# The desktop itself: a screen blank, a lock screen or an Ubuntu update popup lands on top of the
# company's data exactly like an application's own modal does.
DESKTOP_HYGIENE = r"""
set -x
# gsettings writes through dconf-service on the session bus. A plain ssh session has no
# DBUS_SESSION_BUS_ADDRESS, so every set below silently fails to commit and the image keeps the
# stock 5-minute screen blank -- which turns a long episode's screenshot black.
export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.desktop.screensaver lock-enabled false
gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing
gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
gsettings set org.gnome.desktop.notifications show-banners false
gsettings set org.gnome.shell.overrides dynamic-workspaces false 2>/dev/null || true
# Evince opens at a fraction of the screen, so half of every PDF screenshot is wallpaper.
gsettings set org.gnome.Evince.Default window-ratio 1.0 2>/dev/null || true
gsettings set org.gnome.Evince.Default maximized true 2>/dev/null || true
mkdir -p ~/.config/autostart
for desktop in update-notifier gnome-initial-setup-first-login org.gnome.Software; do
  printf '[Desktop Entry]\nType=Application\nName=%s\nHidden=true\nX-GNOME-Autostart-enabled=false\n' \
    "$desktop" > ~/.config/autostart/"$desktop".desktop
done
# A stale lock file baked into the image makes LibreOffice open the company's file read-only.
find ~ -name '.~lock.*#' -delete 2>/dev/null || true
set +x
echo "idle-delay=$(gsettings get org.gnome.desktop.session idle-delay)" \
     "lock-enabled=$(gsettings get org.gnome.desktop.screensaver lock-enabled)" \
     "banners=$(gsettings get org.gnome.desktop.notifications show-banners)"
"""

DESKTOP_HYGIENE_ROOT = r"""
set -x
export DEBIAN_FRONTEND=noninteractive
systemctl disable --now unattended-upgrades 2>/dev/null || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable --now motd-news.timer 2>/dev/null || true
# "Ubuntu Pro" apt news prints into every apt run and pops a Software Updater window.
sed -i 's/^Enabled=1/Enabled=0/' /etc/apt/apt.conf.d/20apt-esm-hook.conf 2>/dev/null || true
rm -f /etc/xdg/autostart/update-notifier.desktop
true
"""


# --------------------------------------------------------------------------------------------
# A minimal QEMU harness. world/vm.py owns the pipeline's launcher; this one exists because
# provisioning needs the opposite of what that class guarantees -- an unrestricted network.
# --------------------------------------------------------------------------------------------


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Guest:
    """A booted qcow2, reachable over ssh, screenshotted through QMP."""

    def __init__(self, disk, work, restrict):
        self.disk, self.work = Path(disk), Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        # Unix sockets stop at 107 bytes and these paths are long, so the socket name is a hash.
        sockets = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "company-envs-desktop"
        sockets.mkdir(parents=True, exist_ok=True)
        self.qmp = sockets / (hashlib.sha256(str(self.work.resolve()).encode()).hexdigest()[:16] + ".qmp")
        self.qmp.unlink(missing_ok=True)
        net = f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.port}-:22"
        if restrict:
            net += ",restrict=on"
        self.command = [
            "qemu-system-x86_64",
            "-enable-kvm",
            "-cpu",
            "host",
            "-m",
            str(MEMORY_MB),
            "-smp",
            "4",
            "-drive",
            f"file={self.disk},if=virtio,format=qcow2",
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
            "file:" + str(self.work / "serial.log"),
            "-netdev",
            net,
            "-device",
            "virtio-net-pci,netdev=n0",
        ]
        self.process = None

    def __enter__(self):
        log = open(self.work / "qemu.log", "ab")
        self.process = subprocess.Popen(self.command, stdout=log, stderr=log, start_new_session=True)
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("qemu exited during boot; see " + str(self.work / "qemu.log"))
            if self.ssh("echo ready", timeout=10, check=False).returncode == 0:
                return self
            time.sleep(5)
        raise RuntimeError(f"guest did not answer ssh within {BOOT_TIMEOUT}s")

    def __exit__(self, *exc):
        self.poweroff()

    def _base(self):
        return [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={self.work / 'known_hosts'}",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "LogLevel=ERROR",
            "-p",
            str(self.port),
            f"{GUEST_USER}@127.0.0.1",
        ]

    def ssh(self, script, timeout=1800, check=True, _raw=False):
        """Scripts travel base64-encoded: shell quoting mangles multi-line payloads."""
        if not _raw:
            blob = base64.b64encode(script.encode()).decode()
            script = f"echo {blob} | base64 -d | bash"
        result = subprocess.run(
            self._base() + [script],
            env=os.environ | {"SSHPASS": GUEST_PASSWORD},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if check and result.returncode:
            raise RuntimeError(
                f"guest command failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
            )
        return result

    def sudo(self, script, **kw):
        # sudo -S takes the password on stdin, so the script has to arrive as a file, not a pipe.
        blob = base64.b64encode(script.encode()).decode()
        return self.ssh(
            f"echo {blob} | base64 -d > /tmp/.provision_step.sh && "
            f"echo {GUEST_PASSWORD} | sudo -S -p '' bash /tmp/.provision_step.sh",
            _raw=True,
            **kw,
        )

    def python(self, source, **kw):
        blob = base64.b64encode(source.encode()).decode()
        return self.ssh(
            f"echo {blob} | base64 -d > /tmp/.provision_step.py && python3 /tmp/.provision_step.py",
            _raw=True,
            **kw,
        )

    def upload(self, local, remote, timeout=1800):
        subprocess.run(
            [
                "sshpass",
                "-e",
                "scp",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                f"UserKnownHostsFile={self.work / 'known_hosts'}",
                "-o",
                "LogLevel=ERROR",
                "-P",
                str(self.port),
                str(local),
                f"{GUEST_USER}@127.0.0.1:{remote}",
            ],
            env=os.environ | {"SSHPASS": GUEST_PASSWORD},
            check=True,
            capture_output=True,
            timeout=timeout,
        )

    def download(self, remote, local, timeout=1800):
        subprocess.run(
            [
                "sshpass",
                "-e",
                "scp",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                f"UserKnownHostsFile={self.work / 'known_hosts'}",
                "-o",
                "LogLevel=ERROR",
                "-P",
                str(self.port),
                f"{GUEST_USER}@127.0.0.1:{remote}",
                str(local),
            ],
            env=os.environ | {"SSHPASS": GUEST_PASSWORD},
            check=True,
            capture_output=True,
            timeout=timeout,
        )

    def poweroff(self, timeout=240):
        if self.process is None or self.process.poll() is not None:
            return True
        self.sudo("systemctl poweroff", timeout=30, check=False)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return True
            time.sleep(2)
        self.process.terminate()
        try:
            self.process.wait(20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        return False


# --------------------------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------------------------


def build(args):
    source, output, work = Path(args.source), Path(args.output), Path(args.work)
    if not source.is_file():
        sys.exit(f"source image not found: {source}")
    if output.exists() and not args.force:
        sys.exit(f"{output} exists; pass --force to rebuild")
    output.parent.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    # The source is a live backing file for real runs. Nothing here may write to it: the guest
    # boots an overlay, and the flatten below reads the source and writes somewhere else.
    overlay = work / "provision.qcow2"
    overlay.unlink(missing_ok=True)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(source.resolve()), str(overlay)],
        check=True,
        capture_output=True,
    )
    print(f"overlay {overlay} on read-only source {source}")

    record = {
        "built": datetime.now(UTC).isoformat(),
        "source": str(source),
        "output": str(output),
        "display": DISPLAY,
        "guest_user": GUEST_USER,
        "steps": [],
        "versions": {},
    }

    def step(name, detail, run):
        started = time.time()
        print(f"  [{name}] {detail}", flush=True)
        out = run()
        record["steps"].append({"step": name, "detail": detail, "seconds": round(time.time() - started, 1)})
        return out

    with Guest(overlay, work / "boot", restrict=False) as guest:
        print("guest up; provisioning")
        step(
            "apt-repo",
            "Microsoft apt repository for VS Code",
            lambda: guest.sudo(f"""
set -e
export DEBIAN_FRONTEND=noninteractive
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
  | gpg --dearmor --yes -o /etc/apt/keyrings/microsoft.gpg
chmod a+r /etc/apt/keyrings/microsoft.gpg
echo '{VSCODE_REPO}' > /etc/apt/sources.list.d/vscode.list
apt-get update -qq
"""),
        )
        packages = " ".join(name for name, _ in APT_PACKAGES) + " code"
        step(
            "apt-install",
            packages,
            lambda: guest.sudo(
                f"export DEBIAN_FRONTEND=noninteractive\napt-get install -y -qq {packages} < /dev/null",
                timeout=3600,
            ),
        )

        step(
            "libreoffice",
            "tip/first-run/recovery suppression in the user profile",
            lambda: print("    " + guest.python(LIBREOFFICE_SUPPRESS).stdout.strip()),
        )
        step(
            "vscode",
            "welcome, Copilot sign-in, trust, telemetry and hot-exit suppression",
            lambda: guest.ssh(
                "mkdir -p ~/.config/Code/User && cat > ~/.config/Code/User/settings.json"
                " <<'JSON'\n" + json.dumps(VSCODE_SETTINGS, indent=2) + "\nJSON"
            ),
        )
        step(
            "vlc",
            "privacy/network dialog and recent-play suppression",
            lambda: guest.ssh(
                "mkdir -p ~/.config/vlc && cat > ~/.config/vlc/vlcrc <<'CONF'\n" + VLCRC + "CONF"
            ),
        )
        step(
            "openshot",
            "tutorial dialog, metrics opt-in and recovery copies",
            lambda: print("    " + guest.python(OPENSHOT_SUPPRESS, timeout=600).stdout.strip()),
        )
        step(
            "blender",
            "splash suppression in userpref",
            lambda: guest.ssh(f"export DISPLAY={DISPLAY}\n{BLENDER_SUPPRESS}", check=False),
        )
        step(
            "desktop",
            "idle/lock/notification and update-popup suppression",
            lambda: (guest.ssh(DESKTOP_HYGIENE, check=False), guest.sudo(DESKTOP_HYGIENE_ROOT, check=False)),
        )
        step(
            "clean",
            "apt caches and stray lock files",
            lambda: guest.sudo(
                "apt-get clean; rm -rf /var/lib/apt/lists/*; "
                f"find /home/{GUEST_USER} -name '.~lock.*#' -delete 2>/dev/null; true"
            ),
        )

        for name, probe in VERSION_PROBES.items():
            got = guest.ssh(probe, check=False, timeout=180).stdout.strip().splitlines()
            record["versions"][name] = got[-1] if got else "(absent)"
        record["disk_free"] = guest.ssh("df -h / | tail -1", check=False).stdout.strip()
        print("  versions: " + json.dumps(record["versions"], indent=2))
        print("  powering down cleanly")

    # Flatten the chain into a standalone image. -c compresses; on a 50 GB virtual disk this is
    # the slow part, and it is worth it for a file every worker overlay reads from.
    convert = (
        ["qemu-img", "convert", "-O", "qcow2"]
        + (["-c"] if args.compress else [])
        + [str(overlay), str(output) + ".partial"]
    )
    print("  " + " ".join(convert), flush=True)
    started = time.time()
    subprocess.run(convert, check=True)
    Path(str(output) + ".partial").replace(output)
    record["steps"].append(
        {"step": "convert", "detail": " ".join(convert), "seconds": round(time.time() - started, 1)}
    )
    record["image_bytes"] = output.stat().st_size
    record["image_info"] = json.loads(
        subprocess.run(
            ["qemu-img", "info", "--output=json", str(output)], check=True, capture_output=True, text=True
        ).stdout
    )
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(record, indent=2) + "\n")
    print(f"\nwrote {output} ({output.stat().st_size / 2**30:.2f} GiB)\nmanifest {manifest}")
    if not args.keep_overlay:
        overlay.unlink(missing_ok=True)
    return 0


# --------------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------------

# (app type, settle seconds, launch command, guest path it opens). The command is exactly what a
# task's initial_setup.py should run, with DISPLAY=:1 supplied by the caller.
PROOFS = [
    # --norestore on every LibreOffice line: it is the only lever that survives both the controller
    # stopping the VM and a worker killing the application. The profile suppression below is belt
    # and braces for the rest.
    (
        "libreoffice_calc",
        25,
        'libreoffice --norestore --calc "{path}"',
        "Downloads/August plant overhead v2.xlsx",
    ),
    (
        "libreoffice_writer",
        25,
        'libreoffice --norestore --writer "{path}"',
        "Desktop/September desk notes.docx",
    ),
    (
        "libreoffice_impress",
        25,
        'libreoffice --norestore --impress "{path}"',
        "Downloads/Forklift refresher September.pptx",
    ),
    ("pdf", 12, 'evince "{path}"', "Documents/Plant governance/Virginia Materials Charter rev3.pdf"),
    ("vscode", 25, 'code "{home}/Documents/alert-fixtures" "{path}"', "Documents/alert-fixtures/router.py"),
    ("gimp", 20, 'gimp "{path}"', "Pictures/harbor-science.png"),
    ("vlc", 12, 'vlc "{path}"', "Videos/Harbor line walkthrough.mp4"),
    ("blender", 30, 'blender "{path}"', "Documents/Design/Harbor enclosure v3.blend"),
    ("openshot", 35, 'openshot-qt "{path}"', "Videos/Harbor line walkthrough.mp4"),
    ("os", 12, 'nautilus "{home}"', ""),
]

# What each application must be killed by name for the unclean-restart half of the proof.
KILL_PATTERNS = {
    "libreoffice_calc": "soffice",
    "libreoffice_writer": "soffice",
    "libreoffice_impress": "soffice",
    "pdf": "evince",
    "vscode": "/usr/share/code/code",
    "gimp": "gimp",
    "vlc": "vlc",
    # "openshot" alone also matches any helper whose path contains the word, which is how the
    # provisioning step once killed its own configuration script mid-write.
    "blender": "blender",
    "openshot": "bin/openshot-qt",
    "os": "nautilus",
}

# mutter keeps two full-screen windows up at all times -- its guard window and the compositor's
# own "@!0,0;BDHF" -- so anything that asks "is something covering the application" sees a modal
# over every app unless both are excluded. (Found by the adapter agent on a live guest.)
SHELL_WINDOWS = r"@!0,0;BDHF|mutter guard window"

GUEST_SHOT = r"""#!/bin/bash
# Launch one application on the company's file, let it settle, then record what is on screen.
export DISPLAY=__DISPLAY__
export XAUTHORITY=$(ls /run/user/1000/gdm/Xauthority 2>/dev/null || echo "$HOME/.Xauthority")
tag="$1"; shift; settle="$1"; shift
mkdir -p "$HOME/shots"
nohup setsid bash -c "$*" >"$HOME/shots/$tag.log" 2>&1 < /dev/null &
sleep "$settle"
# Several applications open at a fraction of the screen -- evince at 600x600 on wallpaper -- which
# reads as a broken app in any visual check. _NET_WORKAREA lies here (it claims 1280x800 while the
# dock and top bar take 70 and 27 px) and xdotool windowstate does not exist on this image, so the
# geometry is fixed with wmctrl, which does work.
wmctrl -l | grep -Ev '__SHELL__' | awk '{print $1}' | while read -r id; do
  wmctrl -i -r "$id" -b add,maximized_vert,maximized_horz 2>/dev/null
done
sleep 3
wmctrl -l | grep -Ev '__SHELL__' > "$HOME/shots/$tag.windows"
import -window root "$HOME/shots/$tag.png" 2>/dev/null
cat "$HOME/shots/$tag.windows"
""".replace("__DISPLAY__", DISPLAY).replace("__SHELL__", SHELL_WINDOWS)


# Typing is the only way to see AutoCorrect: it rewrites input as it is entered, so a
# programmatic insert never reproduces it. Every clause in the marker is something one of the
# AutoCorrect options rewrites.
TYPED_MARKER = 'desktop probe 1789075589 teh 1st -- "q" TWo  x'

TYPED_PROBE = r"""#!/bin/bash
# Open a LibreOffice document, type the marker at the end, save, and leave the file behind.
set -u
export DISPLAY=__DISPLAY__
export XAUTHORITY=$(ls /run/user/1000/gdm/Xauthority 2>/dev/null || echo "$HOME/.Xauthority")
kind="$1"; src="$2"; out="$3"
pkill -9 -f soffice; sleep 4
find "$HOME" -name '.~lock.*#' -delete 2>/dev/null
cp "$src" "$out"
nohup setsid libreoffice --norestore --"$kind" "$out" >/dev/null 2>&1 < /dev/null &
sleep 22
wid=$(wmctrl -l | grep -F "$(basename "$out")" | awk '{print $1}' | head -1)
[ -n "$wid" ] || { echo "NO_WINDOW"; exit 1; }
wmctrl -i -a "$wid"; sleep 2
if [ "$kind" = writer ]; then
  xdotool key --clearmodifiers ctrl+End; sleep 1
  xdotool key --clearmodifiers Return; sleep 1
else
  xdotool key --clearmodifiers ctrl+Home; sleep 1
  for _ in $(seq 9); do xdotool key --clearmodifiers Down; done; sleep 1
fi
xdotool type --clearmodifiers --delay 14 --file "$HOME/typed_marker.txt"
sleep 2
[ "$kind" = writer ] || { xdotool key --clearmodifiers Return; sleep 2; }
xdotool key --clearmodifiers ctrl+s; sleep 6
# A "Confirm File Format" window here means WarnAlienFormat did not take: it is modal, and every
# seeded material is a Microsoft format, so it would block the save on every task.
wmctrl -l | grep -Ev '__SHELL__' || true
pkill -9 -f soffice; sleep 3
"""

TYPED_READBACK = r"""
import html, json, re, sys, zipfile

marker = open("/home/ga/typed_marker.txt", encoding="utf-8").read()
result = {"marker": marker, "documents": {}}
for kind, path in (("writer", sys.argv[1]), ("calc", sys.argv[2])):
    try:
        archive = zipfile.ZipFile(path)
        if kind == "writer":
            xml = archive.read("word/document.xml").decode("utf-8")
            blocks = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
            texts = ["".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", b, re.S)) for b in blocks]
            saved = html.unescape([t for t in texts if t.strip()][-1])
            extra = {"hyperlink_elements": xml.count("<w:hyperlink")}
        else:
            shared = [n for n in archive.namelist() if "sharedStrings" in n]
            xml = archive.read(shared[0]).decode("utf-8") if shared else ""
            cells = [html.unescape(s) for s in re.findall(r"<t[^>]*>(.*?)</t>", xml, re.S)]
            hits = [c for c in cells if "1789075589" in c]
            saved = hits[0] if hits else ""
            extra = {}
        result["documents"][kind] = dict(saved=saved, identical=(saved == marker), **extra)
    except Exception as error:  # a missing file is a failed probe, not a crash
        result["documents"][kind] = {"error": str(error), "identical": False}
print(json.dumps(result))
"""


def typed_round_trip(guest, home, out):
    """Type a marker into Writer and Calc, save, and read the file back.

    AutoCorrect rewrote five things in this marker on the unfixed image -- capitalising the first
    word, `teh`->`the`, `--`->en dash, straight quotes to curly, `TWo`->`Two`. Every one of them
    fails a grader that checks the text it asked the worker to type, over a change the worker did
    not make. A screenshot cannot see this; only reading the saved file back can.
    """
    guest.ssh("printf '%s' " + json.dumps(TYPED_MARKER) + " > ~/typed_marker.txt")
    guest.ssh(
        "cat > ~/typed_probe.sh <<'SH'\n"
        + TYPED_PROBE.replace("__DISPLAY__", DISPLAY).replace("__SHELL__", SHELL_WINDOWS)
        + "SH\nchmod +x ~/typed_probe.sh"
    )
    windows = {}
    for kind, source, target in (
        ("writer", f"{home}/Desktop/September desk notes.docx", f"{home}/typed_probe.docx"),
        ("calc", f"{home}/Downloads/August plant overhead v2.xlsx", f"{home}/typed_probe.xlsx"),
    ):
        print(f"  [typed:{kind}] {TYPED_MARKER!r}", flush=True)
        result = guest.ssh(
            f"~/typed_probe.sh {kind} {json.dumps(source)} {json.dumps(target)}", check=False, timeout=600
        )
        windows[kind] = [line for line in result.stdout.strip().splitlines() if line.strip()]
    # The read-back script takes the two paths as argv, so it goes over as a file with arguments.
    blob = base64.b64encode(TYPED_READBACK.encode()).decode()
    readback = guest.ssh(
        f"echo {blob} | base64 -d > /tmp/.readback.py && "
        f"python3 /tmp/.readback.py {home}/typed_probe.docx {home}/typed_probe.xlsx",
        _raw=True,
        check=False,
        timeout=180,
    )
    try:
        parsed = json.loads(readback.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        parsed = {"error": readback.stdout + readback.stderr}
    parsed["windows_at_save"] = windows
    for kind, info in parsed.get("documents", {}).items():
        state = "IDENTICAL" if info.get("identical") else "REWRITTEN"
        print(f"    {kind}: {state}  saved={info.get('saved', info.get('error'))!r}", flush=True)
    return parsed


def seed_materials(destination):
    """Render real company materials into the files the applications open.

    The repo's own renderer does this at VM launch, so the proof opens exactly what a worker
    would. Media and raster assets are generated: no seeded world ships either (see the report).
    """
    from company_envs.world.documents import guest_path, render_material

    companies = ROOT / "companies"
    picks = [
        "trex-company/world/materials/plant-manager/Downloads/August plant overhead v2.xlsx",
        "trex-company/world/materials/plant-manager/Desktop/September desk notes.docx",
        "trex-company/world/materials/production-supervisor/Downloads/Forklift refresher September.pptx",
        (
            "trex-company/world/materials/plant-manager/Documents/Plant governance/"
            "Virginia Materials Charter rev3.pdf"
        ),
        (
            "chinapro-marketing-partners/world/materials/visual-designer/Documents/Asset library/Harbor/"
            "harbor-science.svg"
        ),
    ]
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    seeded = []
    for relative in picks:
        source = companies / relative
        if not source.is_file():
            continue
        parts = Path(relative).parts
        name = "/".join(parts[parts.index("materials") + 2 :])
        target = destination / guest_path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(render_material(name, source.read_bytes()))
        seeded.append(guest_path(name))

    workspace = companies / (
        "mount-sinai-health-system/world/materials/software-engineer/Documents/alert-fixtures"
    )
    if workspace.is_dir():
        out = destination / "Documents/alert-fixtures"
        out.mkdir(parents=True, exist_ok=True)
        for item in sorted(workspace.iterdir()):
            if item.is_file():
                (out / item.name).write_bytes(render_material(item.name, item.read_bytes()))
        seeded.append("Documents/alert-fixtures")

    png = destination / "Pictures/harbor-science.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    _asset_raster(destination / "Documents/Asset library/Harbor/harbor-science.svg", png)
    seeded.append("Pictures/harbor-science.png")

    video = destination / "Videos/Harbor line walkthrough.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(png),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=20",
            "-t",
            "20",
            "-r",
            "24",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            (
                "scale=1280:720,drawtext=text='Harbor line walkthrough - Trex Company':"
                "fontcolor=white:fontsize=34:box=1:boxcolor=black@0.6:x=(w-tw)/2:y=h-80"
            ),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    seeded.append("Videos/Harbor line walkthrough.mp4")
    return seeded


def _asset_raster(svg, png):
    """A raster carrying the stub SVG's own title, credit and colour.

    The only image materials any seeded world ships are SVG stubs whose single ``<image>`` points
    at a remote URL, so a restricted guest rasterises them to an empty canvas. GIMP opening on
    nothing is not a proof, so the asset's own metadata is drawn instead.
    """
    import re

    from PIL import Image, ImageDraw, ImageFont

    text = svg.read_text() if svg.is_file() else "<title>Company asset</title>"
    title = (re.search(r"<title>(.*?)</title>", text, re.DOTALL) or [None, "Company asset"])[1].strip()
    description = (re.search(r"<desc>(.*?)</desc>", text, re.DOTALL) or [None, ""])[1].strip()
    digest = hashlib.sha256(svg.name.encode()).digest()
    base = tuple(90 + digest[index] % 110 for index in range(3))

    image = Image.new("RGB", (1600, 1000), (246, 244, 239))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 1600, 250], fill=base)
    for index in range(6):
        shade = tuple(min(255, channel + 18 * (index - 2)) for channel in base)
        draw.rectangle([120 + index * 220, 360, 300 + index * 220, 760], fill=shade)
    fonts = "/usr/share/fonts/truetype/dejavu"
    draw.text((60, 90), title, font=ImageFont.truetype(f"{fonts}/DejaVuSans-Bold.ttf", 54), fill="#ffffff")
    small = ImageFont.truetype(f"{fonts}/DejaVuSans.ttf", 26)
    offset = 810
    for line in re.findall(r".{1,110}(?:\s|$)", description):
        draw.text((60, offset), line.strip(), font=small, fill="#2b3138")
        offset += 42
    image.save(png)


def verify(args):
    image, work, out = Path(args.image), Path(args.work), Path(args.out)
    if not image.is_file():
        sys.exit(f"image not found: {image}")
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    seed_dir = out / "seed"
    seeded = seed_materials(seed_dir)
    tarball = out / "seed.tar"
    subprocess.run(["tar", "cf", str(tarball), "-C", str(seed_dir), "."], check=True)
    print(f"seeded {len(seeded)} materials from real company worlds")

    overlay = work / "verify.qcow2"
    overlay.unlink(missing_ok=True)
    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(image.resolve()), str(overlay)],
        check=True,
        capture_output=True,
    )

    report = {
        "image": str(image),
        "display": DISPLAY,
        "restricted_network": True,
        "checked": datetime.now(UTC).isoformat(),
        "seeded": seeded,
        "apps": [],
    }
    home = f"/home/{GUEST_USER}"

    # restrict=on is what the pipeline gives a worker: no route off the host. An application that
    # needs the network to start clean has to fail here, not in an episode.
    with Guest(overlay, work / "boot", restrict=True) as guest:
        guest.upload(tarball, f"{home}/seed.tar")
        guest.ssh(f"cd {home} && tar xf seed.tar && rm seed.tar && mkdir -p {home}/shots")
        guest.ssh("cat > ~/shot.sh <<'SH'\n" + GUEST_SHOT + "SH\nchmod +x ~/shot.sh")
        # Blender is the one application with no seeded file of its type anywhere; build one.
        guest.ssh(
            f"""
export DISPLAY={DISPLAY}
mkdir -p '{home}/Documents/Design'
blender --background --python-expr '
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0,0,1)); bpy.context.object.name="HarborEnclosure"
bpy.ops.mesh.primitive_cylinder_add(radius=0.6, depth=2.4, location=(2.6,0,1.2))
bpy.context.object.name="HarborTank"
bpy.ops.object.camera_add(location=(7,-7,5), rotation=(1.1,0,0.8))
bpy.ops.object.light_add(type="SUN", location=(4,-4,8))
bpy.ops.wm.save_as_mainfile(filepath="{home}/Documents/Design/Harbor enclosure v3.blend")
'""",
            check=False,
            timeout=600,
        )

        for app, settle, template, relative in PROOFS:
            path = f"{home}/{relative}" if relative else home
            command = template.format(path=path, home=home)
            kill = KILL_PATTERNS[app]
            entry = {
                "app": app,
                "command": command,
                "display": DISPLAY,
                "settle_seconds": settle,
                "opens": relative or home,
            }
            print(f"  [{app}] {command}", flush=True)
            guest.ssh(f"pkill -9 -f {kill}; sleep 3", check=False)
            # A glob only reaches one level; materials sit two deep ("Documents/Plant governance/").
            guest.ssh(f"find {home} -name '.~lock.*#' -delete 2>/dev/null; true", check=False)

            first = guest.ssh(
                f"~/shot.sh {app}_open {settle} {json.dumps(command)}", check=False, timeout=settle + 300
            )
            entry["windows_first"] = first.stdout.strip().splitlines()

            # The second launch is the real test: the controller stops episode VMs, so this is the
            # state the next task on a reused desktop actually opens into.
            guest.ssh(f"pkill -9 -f {kill}; sleep 5", check=False)
            second = guest.ssh(
                f"~/shot.sh {app}_reopen {settle} {json.dumps(command)}", check=False, timeout=settle + 300
            )
            entry["windows_after_unclean_kill"] = second.stdout.strip().splitlines()

            for tag in (f"{app}_open", f"{app}_reopen"):
                try:
                    guest.download(f"{home}/shots/{tag}.png", out / f"{tag}.png")
                except subprocess.CalledProcessError:
                    entry.setdefault("missing_screenshots", []).append(tag)
            entry["screenshots"] = [f"{app}_open.png", f"{app}_reopen.png"]
            report["apps"].append(entry)
            guest.ssh(f"pkill -9 -f {kill}; sleep 2", check=False)

        report["typed_round_trip"] = typed_round_trip(guest, home, out)

    (out / "verify.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"\n{len(report['apps'])} applications launched; screenshots and verify.json in {out}")
    print("Look at every screenshot. A window title is not a proof that the data is uncovered.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    make = sub.add_parser("build", help="install and configure into a new image")
    make.add_argument("--source", default=str(DEFAULT_SOURCE))
    make.add_argument("--output", default=str(DEFAULT_OUTPUT))
    make.add_argument("--work", default=str(DEFAULT_WORK))
    make.add_argument("--force", action="store_true", help="overwrite an existing output image")
    make.add_argument(
        "--no-compress",
        dest="compress",
        action="store_false",
        help="skip qemu-img convert -c (faster, larger)",
    )
    make.add_argument("--keep-overlay", action="store_true")
    make.set_defaults(func=build, compress=True)

    check = sub.add_parser("verify", help="open every application on real company files and screenshot")
    check.add_argument("--image", default=str(DEFAULT_OUTPUT))
    check.add_argument("--work", default=str(DEFAULT_WORK))
    check.add_argument("--out", default="/mnt/storage/desktop-probe/verify")
    check.set_defaults(func=verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
