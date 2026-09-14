"""Launch disposable hub desktops from a served company folder.

``worker_apps.json`` is the company-wide worker -> app-list contract. Its workers
extend the task roster unless an explicit subset is supplied. QEMU's restricted
user network forwards only assigned apps through host-side ``nc`` to their proxies on
``host_ip``. Each allowed app gets its own guest address (10.0.2.101, 10.0.2.102, ... on
port 80) and hostname (``gmail.<company>.internal``); ordinary host/network access is blocked.

Only ``guest/`` below each VM directory is uploaded. Workflow designs, sessions,
endpoints, and launch/process receipts remain on the host. Stop/reset work from
another process using Linux process birth identifiers, never broad process kills.
Reset restores the ready desktop snapshot; it does not reset the shared hub app state.
"""

import hashlib
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tarfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from company_envs.storage import now, read, run_lock, write

from .documents import guest_path, render_material
from .vm import SNAPSHOT_NAME, WorkerVM, qmp_socket_path

_FIRST_APP_IP = 101  # 10.0.2.101, 10.0.2.102, ... one guest address per allowed app
# A guest is a materially slower machine than the host: measured 2026-09-09/10, the same app that
# hydrates in four seconds on the host shows 3,172 characters and no seeded strings at four seconds
# in a guest and the host's own 198,993 at twelve, one guest reads a 6.0 MB state in 2.2 s while
# three concurrent guests take 20-23 s for the same read, and the single host serve process hosting
# 51 proxies and 17 vite servers sits at 89% CPU. The old readiness check capped each attempt at
# three seconds, so an app whose first response took longer than that could never be declared ready
# no matter how many times it retried. The cap is now longer than the slowest measured first
# response, and the retry loop is shell rather than curl flags so it does not depend on the guest
# curl being new enough for --retry-all-errors.
READY_MAX_SECONDS = 20  # per attempt
READY_ATTEMPTS = 6
READY_DELAY = 2
# How long a guest needs, after Chrome answers CDP, before its app tabs hold the company's records.
# The render gate measures hydration on the host and publishes the guest figure in RENDER.json
# because its own browser never runs in a guest; this is the fallback when no report is at hand.
# Measured: bamboohr at a four-second settle shows 3,172 characters and no seeded strings in a guest
# and 198,993 -- the host's own figure -- at twelve. The launch used to declare a worker ready the
# moment the start page appeared, so a screenshot or a saved snapshot could capture a desktop of
# unhydrated apps.
GUEST_SETTLE_SECONDS = 12
GUEST_SETTLE_MAX = 120
GUEST_PASSWORD = "password123"  # Public credential of the local base image (see vm.py).
SAVE_SNAPSHOT = os.environ.get("COMPANY_ENVS_VM_SNAPSHOT") == "1"
START_PAGE = "file:///home/ga/Desktop/APPS.html"
# Human names and one-line purposes for the well-known apps; others derive from the id.
_APP_LABELS = {
    "gmail_mock": ("Gmail", "Email"),
    "google_calendar_mock": ("Calendar", "Meetings and schedules"),
    "google_docs_mock": ("Docs", "Documents and memos"),
    "google_drive_mock": ("Drive", "Shared files and folders"),
    "google_sheets_mock": ("Sheets", "Spreadsheets"),
    "slack_mock": ("Slack", "Team chat"),
    "tableau_mock": ("Tableau", "Dashboards and reports"),
    "outlook_web_mock": ("Outlook", "Email and calendar"),
}
_START_PAGE_STYLE = (
    "body{margin:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;background:#f3f4f6;"
    "color:#1f2937}header{background:#1e3a5f;color:#fff;padding:28px 40px}header h1{margin:0;font-size:24px;"
    "font-weight:600}header p{margin:6px 0 0;opacity:.85}main{max-width:960px;margin:32px auto;padding:0 24px}"
    "h2{font-size:15px;color:#4b5563;font-weight:600;margin:0 0 14px;text-transform:uppercase;"
    "letter-spacing:.04em}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}"
    ".tile{display:block;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;"
    "text-decoration:none;color:inherit;box-shadow:0 1px 2px rgba(0,0,0,.05)}.tile:hover{border-color:#1e3a5f;"
    "box-shadow:0 2px 8px rgba(0,0,0,.1)}.tile b{display:block;font-size:17px;margin-bottom:4px}"
    ".tile span{color:#6b7280;font-size:13px}footer{margin-top:36px;color:#6b7280;font-size:13px}"
)


def app_label(app_id):
    """(human name, one-line purpose) for an app id; unknown ids are title-cased without ``_mock``."""
    if app_id in _APP_LABELS:
        return _APP_LABELS[app_id]
    name = re.sub(r"[_-]?mock$", "", app_id).replace("_", " ").replace("-", " ").strip()
    return (name.title() or app_id, "Company application")


def host_label(app_id):
    """The hostname label of an app: its human name in lowercase letters and digits."""
    return re.sub(r"[^a-z0-9]+", "", app_label(app_id)[0].lower()) or "app"


def company_slug(company):
    """A DNS label for the company from its name, else its id, else ``company``."""
    for value in (company.get("name"), company.get("id")):
        if isinstance(value, str):
            slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:63].strip("-")
            if slug:
                return slug
    return "company"


def address_plan(apps, slug):
    """Guest addresses for a worker's allowed apps: each on port 80 at its own IP and hostname."""
    plan = {}
    for index, app in enumerate(apps):
        host = f"{host_label(app)}.{slug}.internal"
        if any(entry["host"] == host for entry in plan.values()):
            raise ValueError(f"Apps share the guest hostname {host}")
        if _FIRST_APP_IP + index > 254:
            raise ValueError("Too many apps for one worker's guest network")
        plan[app] = {"host": host, "ip": f"10.0.2.{_FIRST_APP_IP + index}"}
    return plan


def _worker_name(identities, worker):
    """The worker's display name from any seeded app identity, or None."""
    for record in (identities.get(worker) or {}).values():
        if isinstance(record, dict):
            for key in ("fullName", "name", "username", "displayName"):
                if isinstance(record.get(key), str) and record[key].strip():
                    return record[key].strip()
    return None


def apps_note(apps):
    """The short "Your apps" section shared by ASSIGNMENT.md and ROLE.md; no addresses."""
    lines = "\n".join(f"- {app_label(app)[0]} \u2014 {app_label(app)[1]}" for app in apps)
    return (
        f"## Your apps\n\n{lines}\n\n"
        "They are open as browser tabs and pinned in the Company apps bookmarks bar.\n"
    )


def start_page(company_name, person, title, urls):
    """The browser start page: an intranet-style launcher with one tile per assigned app."""
    tiles = "".join(
        f'<a class="tile" href="{html.escape(url, quote=True)}"><b>{html.escape(app_label(app)[0])}</b>'
        f"<span>{html.escape(app_label(app)[1])}</span></a>"
        for app, url in urls.items()
    )
    who = html.escape(person if person == title else f"{person} \u00b7 {title}")
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(company_name)} '
        f"\u2013 Home</title><style>{_START_PAGE_STYLE}</style></head><body>"
        f"<header><h1>{html.escape(company_name)}</h1><p>Signed in as {who}</p></header>"
        f'<main><h2>Your apps</h2><div class="grid">{tiles}</div>'
        "<footer>These apps are also open as browser tabs and pinned in the bookmarks bar. "
        "Your files are in the Desktop, Documents and Downloads folders.</footer></main></body></html>\n"
    ).encode()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
        raise ValueError(f"Invalid worker/task identifier: {value!r}")
    return value


def _split_proxy_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or not parsed.port
        or parsed.path.rstrip("/") in {"/go", "/post", "/state"}
    ):
        raise ValueError("Expected a worker-facing proxy URL with an explicit port")
    return parsed


def _proxy_url(url, host_ip):
    """The proxy URL as reachable on ``host_ip`` (used for leak detection only)."""
    parsed = _split_proxy_url(url)
    host = f"[{host_ip}]" if ":" in host_ip else host_ip
    return urlunsplit(parsed._replace(netloc=f"{host}:{parsed.port}"))


def _guest_url(url, host):
    """The proxy URL as the guest sees it: plain http on port 80 at the app's hostname."""
    parsed = _split_proxy_url(url)
    return urlunsplit(parsed._replace(scheme="http", netloc=host))


def _files(root):
    if any(p.is_symlink() for p in (root, *root.parents)) or not root.is_dir():
        raise ValueError(f"Expected a regular directory: {root}")
    paths = sorted(root.rglob("*"))
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in paths):
        raise ValueError(f"Only regular files/directories may be delivered: {root}")
    return [p for p in paths if p.is_file()]


def _archive(root, target):
    with tarfile.open(target, "w") as stream:
        for path in _files(root):
            stream.add(path, arcname=path.relative_to(root), recursive=False)


def _plan(folder, task_id, host_ip, workers):
    endpoint_path = folder / "runtime/endpoints.json"
    if not endpoint_path.is_file():
        raise FileNotFoundError(f"Missing {endpoint_path}; run hub-serve first")
    apps = read(endpoint_path)["apps"]
    read(folder / "runtime/sessions.json")  # Require the completed hub-serve handoff.
    workflow = read(folder / "tasks" / _identifier(task_id) / "workflow.json")
    assignment = read(folder / "tasks" / task_id / "assignment.json")
    worker_apps = read(folder / "world/worker_apps.json")
    task_workers = [_identifier(w) for w in workflow["worker_ids"]]
    if not task_workers:
        raise ValueError("Task has no workers")
    assumed = workflow.get("manager_id") is None
    boss = _identifier(workflow.get("manager_id") or task_workers[0])
    eligible = list(dict.fromkeys([*task_workers, boss, *worker_apps]))
    if isinstance(workers, (str, bytes)):
        raise TypeError("workers must be a sequence of worker identifiers")
    selected = list(dict.fromkeys(workers)) if workers is not None else eligible
    if not selected or boss not in selected or set(selected) - set(eligible):
        raise ValueError("Selected workers must be known and must include the boss")
    brief = assignment.get("brief")
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("assignment.json must contain a nonempty public brief")
    company_path = folder / "company.json"
    company = read(company_path) if company_path.exists() else {}
    titles = {w["id"]: w["title"] for w in company.get("workers", [])}
    company_name = company["name"] if isinstance(company.get("name"), str) and company["name"] else "Company"
    slug = company_slug(company)
    identities_path = folder / "world/identities.json"
    identities = read(identities_path) if identities_path.is_file() else {}
    forbidden = {"harness_url"}
    private_hashes = {
        hashlib.sha256(p.read_bytes()).digest() for p in (folder / "tasks").rglob("*") if p.is_file()
    }
    private_ports = set()
    for entry in apps.values():
        if entry.get("harness_url"):
            forbidden.add(entry["harness_url"])
            private_ports.add(urlsplit(entry["harness_url"]).port)
            # Catch a material that already rewrote a harness host for the guest.
            forbidden.add(_proxy_url(entry["harness_url"], host_ip))
    plans = {}
    for worker in selected:
        _identifier(worker)
        allowed = worker_apps.get(worker)
        if not isinstance(allowed, list) or not allowed or not all(isinstance(a, str) for a in allowed):
            raise ValueError(f"Missing/nonempty app list required for worker {worker}")
        addresses = address_plan(allowed, slug)
        urls = {}
        for app in allowed:
            raw = apps.get(app, {}).get("workers", {}).get(worker)
            if not isinstance(raw, str):
                raise ValueError(f"Missing proxy endpoint for {worker}/{app}")  # noqa: TRY004
            urls[app] = _guest_url(raw, addresses[app]["host"])
            addresses[app]["proxy_port"] = urlsplit(raw).port
        if (folder / "world/POPULATION.json").exists():
            from .population_amendments import desktop_root
            root = desktop_root(folder) / worker
        else:
            root = folder / "world/materials" / worker
        materials = {str(p.relative_to(root)): p.read_bytes() for p in _files(root)}
        if any(hashlib.sha256(content).digest() in private_hashes for content in materials.values()):
            raise ValueError(f"Materials for {worker} contain a private task file")
        if any(Path(name).name.upper() == "ASSIGNMENT.MD" for name in materials):
            raise ValueError("ASSIGNMENT.md is reserved for the public boss brief")
        # Keep source bytes in the plan: rendered Office files contain timestamps
        # that would otherwise make an unchanged desktop miss its saved snapshot.
        # Materials under Desktop/, Documents/, Downloads/, Pictures/ or Music/ land in
        # those home folders; anything else lands on the Desktop.
        desktop = {guest_path(name): content for name, content in materials.items()}
        if len(desktop) != len(materials):
            raise ValueError(f"Materials for {worker} collide on the guest")
        if any(name in desktop for name in ("Desktop/ROLE.md", "Desktop/APPS.html")):
            raise ValueError("ROLE.md and APPS.html are reserved launcher files")
        onboarding = next(
            (
                content.decode(errors="replace").strip().splitlines()[0].lstrip("# ")
                for name, content in materials.items()
                if "onboarding" in name.lower() and content.strip()
            ),
            worker,
        )
        title = titles.get(worker, onboarding)
        if worker == boss:
            text = f"# {assignment.get('title', 'Assignment')}\n\n{brief.strip()}\n\n{apps_note(allowed)}"
            desktop["Desktop/ASSIGNMENT.md"] = text.encode()
        else:
            text = (
                f"# {title}\n\n"
                "Read your onboarding and desktop materials. Await delegation from your manager "
                f"through the company's chat/email apps.\n\n{apps_note(allowed)}"
            )
            desktop["Desktop/ROLE.md"] = text.encode()
        desktop["Desktop/APPS.html"] = start_page(
            company_name, _worker_name(identities, worker) or title, title, urls
        )
        preferences = read(Path(__file__).with_name("browser_preferences.json")) | {
            "homepage": START_PAGE,
            "homepage_is_newtabpage": False,
            # Every assigned app opens in its own tab behind the start page.
            "session": {"restore_on_startup": 4, "startup_urls": [START_PAGE, *urls.values()]},
            "bookmark_bar": {"show_on_all_tabs": True},
        }
        roots = {
            key: {"type": "folder", "name": name, "children": []}
            for key, name in (("bookmark_bar", "Company apps"), ("other", "Other"), ("synced", "Mobile"))
        }
        roots["bookmark_bar"]["children"] = [
            {"type": "url", "name": app_label(app)[0], "url": url} for app, url in urls.items()
        ]
        desktop["worker-browser/Default/Preferences"] = json.dumps(preferences).encode()
        desktop["worker-browser/Default/Bookmarks"] = json.dumps({"version": 1, "roots": roots}).encode()
        denied = forbidden.copy()
        denied_ports = private_ports.copy()
        for app, entry in apps.items():
            for who, raw in entry.get("workers", {}).items():
                if who != worker or app not in allowed:
                    denied.update((raw, _proxy_url(raw, host_ip)))
                    denied_ports.add(urlsplit(raw).port)
            if app not in allowed:
                denied.add(f"{host_label(app)}.{slug}.internal")
        if any(value.encode() in content for value in denied for content in desktop.values()):
            raise ValueError(f"VM-bound content for {worker} contains a private/unassigned URL")
        # URLs may omit the query, use another host alias or be HTML/JSON escaped.
        # All registered hub ports belong to this host, so compare their ports as
        # well as literal strings, and allow only this worker's guest app addresses.
        # This is leak detection; the restricted VM network below is the enforcement boundary.
        own_ips = {entry["ip"] for entry in addresses.values()}
        for content in desktop.values():
            text = html.unescape(content.decode(errors="replace")).replace("\\/", "/")
            for raw in re.findall(r"https?://[^\s<>\"']+", text, re.IGNORECASE):
                if urlsplit(raw).port in denied_ports:
                    raise ValueError(f"VM-bound content for {worker} contains a private/unassigned URL")
            for ip in re.findall(r"(?<![\d.])10\.0\.2\.(\d{1,3})(?![\d.])", text):
                if int(ip) >= 100 and f"10.0.2.{ip}" not in own_ips:
                    raise ValueError(f"VM-bound content for {worker} contains a private/unassigned URL")
        plans[worker] = {
            "urls": urls,
            "addresses": addresses,
            "files": desktop,
            "materials": sorted(guest_path(name) for name in materials),
        }
    return boss, assumed, plans


class HubWorkerVM(WorkerVM):
    """Reuse the overlay, SSH and desktop transport with hub-specific networking."""

    def __init__(self, directory, worker, base_image, clock=None):
        super().__init__(directory, worker, base_image, app_port=1, clock=clock)
        self.command[self.command.index("-netdev") + 1] = (
            f"user,id=n0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22"
        )

    def open_browser(self, urls=(), hosts=None):
        """Start Chrome on the start page with each assigned app in its own tab.

        ``hosts`` maps app hostnames to guest IPs; Chrome resolves them itself so the
        names work even when /etc/hosts could not be written.
        """
        libraries = self.run("test -x /home/ga/browser/chrome && ldd /home/ga/browser/chrome")
        if "not found" in libraries.stdout:
            raise RuntimeError(f"{self.worker}: missing browser libraries")
        displays = self.run("ls /tmp/.X11-unix").stdout.split()
        display = next((":" + d[1:] for d in displays if re.fullmatch(r"X\d+", d)), None)
        if display is None:
            raise RuntimeError("No X desktop in base image")
        # Command-line URLs take precedence over the startup_urls preference, so the
        # app tabs are passed here too; --test-type hides Chrome for Testing's infobar.
        tabs = " ".join(shlex.quote(url) for url in (START_PAGE, *urls))
        resolver = ""
        if hosts:
            rules = ",".join(f"MAP {host} {ip}" for host, ip in hosts.items())
            resolver = shlex.quote(f"--host-resolver-rules={rules}") + " "
        self.run(
            f"DISPLAY={display} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
            "nohup /home/ga/browser/chrome --no-first-run --no-default-browser-check --test-type "
            f"--remote-debugging-port=9222 --password-store=basic {resolver}"
            f"--user-data-dir=/home/ga/worker-browser {tabs} "
            ">/home/ga/worker-browser.log 2>&1 </dev/null &"
        )
        return self.connect_browser()

    def connect_browser(self):
        """Connect to the existing guest browser and wait for its homepage."""
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
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.tunnel.poll() is not None:
                raise RuntimeError("Browser SSH tunnel exited")
            try:
                with urlopen(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=2) as response:
                    if any(p.get("url") == START_PAGE for p in json.load(response)):
                        return self.cdp_port
            except (OSError, URLError, ValueError):
                pass
            time.sleep(0.2)
        raise TimeoutError("Guest browser homepage did not become ready")


def guest_settle(folder):
    """Seconds to wait for a guest's pages to hydrate: the render report's figure, else the default."""
    try:
        published = read(Path(folder) / "runtime" / "RENDER.json").get("guest_settle_seconds")
    except (OSError, ValueError, KeyError, AttributeError):
        published = None
    if (
        isinstance(published, (int, float))
        and not isinstance(published, bool)
        and 0 < published <= GUEST_SETTLE_MAX
    ):
        return float(published)
    return float(GUEST_SETTLE_SECONDS)


def app_ready_command(url, host, ip):
    """The guest shell command that waits for one app to answer, and reports why if it never does.

    Retried in the shell, not with curl's own flags: a stale handoff fails with a connection
    *reset*, not refused, because QEMU's guestfwd accepts the guest's connection before its
    host-side ``nc`` can fail, and ``--retry-connrefused`` does not cover a reset. The last attempt
    runs outside the loop so its message is the one the launcher raises.
    """
    curl = (
        "curl --fail --silent --show-error "
        f"--max-time {READY_MAX_SECONDS} --resolve {host}:80:{ip} {shlex.quote(url)}"
    )
    return (
        f"for attempt in $(seq 1 {READY_ATTEMPTS - 1}); do "
        f"{curl} >/dev/null && exit 0; sleep {READY_DELAY}; done; {curl} >/dev/null"
    )


def live_proxy_ports(plans):
    """Worker/app endpoints from runtime/endpoints.json that nothing on the host is serving.

    Verified before a single guest boots, because a stale handoff is indistinguishable from a broken
    app once the guest is the one asking: guestfwd accepts first, so the guest sees a reset. One VM
    run was spent on a runtime/endpoints.json left behind by an earlier ``hub-serve --check``.
    """
    dead, seen = [], {}
    for worker, plan in plans.items():
        for app, address in plan["addresses"].items():
            port = address.get("proxy_port")
            if port is None:
                dead.append(f"{worker}/{app}: no proxy port")
                continue
            if port not in seen:
                with socket.socket() as probe:
                    probe.settimeout(2)
                    seen[port] = probe.connect_ex(("127.0.0.1", int(port))) == 0
            if not seen[port]:
                dead.append(f"{worker}/{app}:{port}")
    return dead


def hosts_lines(addresses):
    """/etc/hosts lines for a worker's app addresses."""
    return "".join(f"{entry['ip']} {entry['host']}\n" for entry in addresses.values())


def _write_hosts(vm, addresses):
    """Best effort: append the app names to the guest's /etc/hosts through sudo.

    The base image documents no root path, so failure is recorded rather than fatal; the
    browser resolves the names itself and the readiness checks pin them with --resolve.
    """
    command = f"printf '%s\\n' {shlex.quote(GUEST_PASSWORD)} | sudo -S -p '' sh -c " + shlex.quote(
        f"printf '%s' {shlex.quote(hosts_lines(addresses))} >> /etc/hosts"
    )
    result = vm.run(command, check=False)
    return getattr(result, "returncode", None) == 0


def _process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except FileNotFoundError:
        return None


def _identifiers(vm, directory):
    return {
        "overlay": str(directory / "worker.qcow2"),
        "command": vm.command if vm else None,
        "qmp": str(qmp_socket_path(directory)),
        "ssh_port": vm.ssh_port if vm else None,
        "cdp_port": vm.cdp_port if vm else None,
        "process": _process_identity(vm.process.pid) if vm and vm.process else None,
        "tunnel": _process_identity(vm.tunnel.pid) if vm and vm.tunnel else None,
    }


def _preflight(base_image, browser_dir, directory, plans):
    """Everything that must be true before a guest boots: the image, the tools, and a live handoff."""
    if not base_image.is_file() or not os.access(browser_dir / "chrome", os.X_OK):
        raise ValueError("Existing qcow2 base image and executable browser_dir/chrome are required")
    _files(browser_dir)
    for binary in ("qemu-system-x86_64", "qemu-img", "sshpass", "ssh", "scp", "nc"):
        if shutil.which(binary) is None:
            raise ValueError(f"Missing required local executable: {binary}")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise ValueError("Readable/writable KVM is required")
    for worker in plans:
        if len(os.fsencode(qmp_socket_path(directory / worker))) > 107:
            raise ValueError("QMP socket path exceeds 107 bytes; set XDG_RUNTIME_DIR to a short directory")
    if dead := live_proxy_ports(plans):
        raise ConnectionRefusedError(
            "stale hub-serve handoff: runtime/endpoints.json names proxy ports nothing is serving "
            f"({', '.join(dead[:6])}); serve the company world again before launching"
        )


def reference_instant(folder):
    """The world's reference date as a guest clock setting, 09:00 local on that day."""
    seed = Path(folder) / "world" / "SEED.json"
    if not seed.is_file():
        return None
    reference = str(read(seed).get("reference_date") or "")[:19]
    if len(reference) < 10:
        return None
    return reference if "T" in reference else f"{reference}T09:00:00"


def _snapshot_key(boss, plans, clock):
    # Ports and session URLs are part of the saved browser and QEMU network, so a
    # changed hub handoff needs a fresh desktop just as changed materials do.
    payload = {
        "boss": boss,
        "clock": clock,
        "workers": {
            worker: {
                "urls": plan["urls"],
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in plan["files"].items()},
            }
            for worker, plan in plans.items()
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _has_snapshot(target):
    """Inspect the overlay only after QEMU has stopped and released its lock."""
    disk = target / "worker.qcow2"
    if not disk.is_file():
        return False
    result = subprocess.run(
        ["qemu-img", "info", "--output=json", str(disk)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return any(
        snapshot.get("name") == SNAPSHOT_NAME and snapshot.get("vm-state-size", 0) > 0
        for snapshot in json.loads(result.stdout).get("snapshots", [])
    )


def launch_company(folder, task_id, *, base_image, browser_dir, host_ip, workers=None, dry_run=False):
    """Return LAUNCH.json's receipt; leave VMs running until stop/reset.

    Dry runs stage the identical guest payload without requiring a local image,
    browser distribution, QEMU or KVM. Process/port identifiers remain null.
    boot_seconds totals each worker's startup through browser readiness;
    snapshot_seconds records the separate, one-time snapshot saves.
    """
    folder = Path(folder).resolve()
    with run_lock(folder / "runtime"):
        return _launch(folder, task_id, base_image, browser_dir, host_ip, workers, dry_run)


def _launch(folder, task_id, base_image, browser_dir, host_ip, workers, dry_run):
    address = ipaddress.ip_address(host_ip)
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("host_ip must be a guest-reachable host address")
    host_ip = str(address)
    boss, assumed, plans = _plan(folder, task_id, host_ip, workers)
    directory = folder / "runtime/vms"
    if directory.exists():
        raise FileExistsError(f"{directory} already exists; use reset_company to restore the desktop")
    base_image, browser_dir = Path(base_image).resolve(), Path(browser_dir).resolve()
    if not dry_run:
        _preflight(base_image, browser_dir, directory, plans)
    directory.mkdir(mode=0o700)
    report = {
        "task": task_id,
        "boss": boss,
        "workers": list(plans),
        "started_at": now(),
        "boss_assumption": "manager_id is null/missing; first task worker is boss" if assumed else None,
        "dry_run": dry_run,
        "status": "starting",
        "boot_seconds": None if dry_run else 0.0,
        "snapshot_seconds": None if dry_run else 0.0,
        "reset_seconds": None,
        "inputs": {
            "base_image": str(base_image),
            "browser_dir": str(browser_dir),
            "host_ip": host_ip,
            "workers": list(plans),
            "dry_run": dry_run,
        },
    }
    clock = reference_instant(folder)
    report["clock"] = clock
    report["snapshot_key"] = _snapshot_key(boss, plans, clock)
    write(directory / "LAUNCH.json", report)
    vms = []
    try:
        if not dry_run:
            _archive(browser_dir, directory / "browser.tar")
        for worker, plan in plans.items():
            vm = None
            target = directory / worker
            if dry_run:
                target.mkdir()
            else:
                vm = HubWorkerVM(directory, worker, base_image, clock=clock)
                vms.append(vm)
                # NAT otherwise exposes host loopback (including harness ports)
                # and every worker proxy. Allow only this worker's assigned ports,
                # with a fresh host-side connection per guest TCP connection.
                network = f"user,id=n0,restrict=on,ipv6=off,hostfwd=tcp:127.0.0.1:{vm.ssh_port}-:22"
                for address in plan["addresses"].values():
                    # guestfwd's command runs on the host: dial the proxy on host loopback,
                    # not the guest-facing app address used in the worker's bookmarks.
                    command = shlex.join(
                        [shutil.which("nc") or "nc", "127.0.0.1", str(address["proxy_port"])]
                    )
                    network += f",guestfwd=tcp:{address['ip']}:80-cmd:{command.replace(',', ',,')}"
                vm.command[vm.command.index("-netdev") + 1] = network
            receipt = {
                "worker": worker,
                "boss": worker == boss,
                "urls": plan["urls"],
                "addresses": plan["addresses"],
                "materials_delivered": [f"/home/ga/{p}" for p in plan["materials"]],
                "dry_run": dry_run,
                "status": "staged",
                "vm": _identifiers(vm, target),
            }
            write(target / "vm.json", receipt)
            try:
                for name, content in plan["files"].items():
                    path = target / "guest" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    # The company keeps readable text; the desktop gets the PDF,
                    # workbook or document promised by each material's filename.
                    path.write_bytes(render_material(name, content))
                if vm:
                    started = time.monotonic()
                    vm.boot()
                    receipt["vm"] = _identifiers(vm, target)
                    write(target / "vm.json", receipt)
                    _archive(target / "guest", target / "guest.tar")
                    vm.upload(target / "guest.tar", "/home/ga/hub-guest.tar")
                    vm.upload(directory / "browser.tar", "/home/ga/hub-browser.tar")
                    vm.run(
                        "test ! -e /home/ga/worker-browser && test ! -e /home/ga/browser "
                        "&& test ! -e /home/ga/Desktop/ASSIGNMENT.md "
                        "&& mkdir /home/ga/browser "
                        "&& tar -xf /home/ga/hub-browser.tar -C /home/ga/browser "
                        "&& tar -xf /home/ga/hub-guest.tar -C /home/ga "
                        "&& rm /home/ga/hub-guest.tar /home/ga/hub-browser.tar",
                        timeout=120,
                    )
                    receipt["hosts_written"] = _write_hosts(vm, plan["addresses"])
                    for app, url in plan["urls"].items():
                        address = plan["addresses"][app]
                        vm.run(
                            app_ready_command(url, address["host"], address["ip"]),
                            timeout=READY_ATTEMPTS * (READY_MAX_SECONDS + READY_DELAY) + 30,
                        )
                    vm.open_browser(
                        plan["urls"].values(), {a["host"]: a["ip"] for a in plan["addresses"].values()}
                    )
                    # CDP answering and the start page appearing say the browser is up, not that the
                    # app tabs hold the world. Waited on the guest's own clock, so the wait is the
                    # guest's, and before any snapshot: a snapshot of unhydrated apps freezes them.
                    settle = guest_settle(folder)
                    vm.run(f"sleep {settle:g}", timeout=settle + 30)
                    receipt["settle_seconds"] = settle
                    receipt["boot_seconds"] = time.monotonic() - started
                    report["boot_seconds"] += receipt["boot_seconds"]
                    if SAVE_SNAPSHOT:
                        # A saved snapshot writes the guest's RAM into the overlay (gigabytes
                        # per worker), so it is opt-in; without it a reset boots fresh.
                        started = time.monotonic()
                        vm.save_snapshot()
                        receipt["snapshot"] = SNAPSHOT_NAME
                        report["snapshot_seconds"] += time.monotonic() - started
                receipt["status"] = "dry_run" if dry_run else "running"
            finally:
                receipt["vm"] = _identifiers(vm, target)
                write(target / "vm.json", receipt)
        report["status"] = "dry_run" if dry_run else "running"
    except BaseException:
        report["status"] = "failed"
        report["cleanup_errors"] = []
        for vm in vms:
            try:
                vm.close()
            except Exception as exc:  # noqa: BLE001 -- attempt every owned VM, preserve launch error
                report["cleanup_errors"].append(f"{vm.worker}: {exc}")
        raise
    finally:
        write(directory / "LAUNCH.json", report)
    return report


def _stop_process(identity):
    if not identity:
        return
    pid = identity["pid"]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if _process_identity(pid) != identity:
            break  # Already stopped, PID reused, or host rebooted.
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 10
        while _process_identity(pid) == identity and time.monotonic() < deadline:
            time.sleep(0.1)
    if _process_identity(pid) == identity:
        raise TimeoutError(f"VM process {pid} did not stop")
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # A different invocation owns the child.


def _stop(folder):
    directory = folder / "runtime/vms"
    if not (directory / "LAUNCH.json").exists():
        return {"status": "stopped", "workers": []}
    report = read(directory / "LAUNCH.json")
    errors = []
    for worker in report["workers"]:
        path = directory / _identifier(worker) / "vm.json"
        if not path.exists():
            continue  # Launch may have failed before constructing this worker.
        receipt = read(path)
        worker_errors = []
        for kind in ("tunnel", "process"):
            try:
                _stop_process(receipt["vm"].get(kind))
            except OSError as exc:
                worker_errors.append(f"{worker}/{kind}: {exc}")
        errors.extend(worker_errors)
        receipt["status"] = "stop_failed" if worker_errors else "stopped"
        write(path, receipt)
    report.update(status="stop_failed" if errors else "stopped", stopped_at=now())
    write(directory / "LAUNCH.json", report)
    if errors:
        raise RuntimeError("; ".join(errors))
    return report


def stop_company(folder):
    """Stop only the processes recorded by this company's launch; retain artifacts."""
    folder = Path(folder).resolve()
    with run_lock(folder / "runtime"):
        return _stop(folder)


def _restore(directory, report):
    report.update(status="resetting", started_at=now(), reset_mode="snapshot", reset_seconds=None)
    report.pop("stopped_at", None)
    report.pop("cleanup_errors", None)
    write(directory / "LAUNCH.json", report)
    vms = []
    try:
        for worker in report["workers"]:
            target = directory / worker
            receipt = read(target / "vm.json")
            vm = HubWorkerVM.from_snapshot(directory, worker, receipt["vm"])
            vms.append(vm)
            receipt["status"] = "starting"
            started = time.monotonic()
            try:
                vm.boot(snapshot=True)
                # Host TCP connections do not survive a restored guest. Rebuild
                # the tunnel, but keep the browser and its saved desktop running.
                vm.connect_browser()
                receipt["status"] = "running"
                receipt["reset_seconds"] = time.monotonic() - started
            finally:
                receipt["vm"] = _identifiers(vm, target)
                write(target / "vm.json", receipt)
            if (target / "trace").exists():
                shutil.rmtree(target / "trace")
        report["status"] = "running"
    except BaseException:
        report["status"] = "failed"
        report["cleanup_errors"] = []
        for vm in vms:
            try:
                vm.close()
            except Exception as exc:  # noqa: BLE001 -- try every VM and preserve the restore error
                report["cleanup_errors"].append(f"{vm.worker}: {exc}")
        raise
    finally:
        write(directory / "LAUNCH.json", report)
    return report


def reset_company(folder):
    """Restore saved desktops, or boot fresh when the saved setup no longer applies.

    reset_seconds includes validation, stopping old processes and desktop readiness.
    """
    folder = Path(folder).resolve()
    with run_lock(folder / "runtime"):
        started = time.monotonic()
        directory = folder / "runtime/vms"
        previous = read(directory / "LAUNCH.json")
        inputs = previous["inputs"]
        # Validate the current handoff before stopping or discarding a launch.
        boss, _, plans = _plan(folder, previous["task"], inputs["host_ip"], inputs["workers"])
        key = _snapshot_key(boss, plans, reference_instant(folder))
        reusable = (
            not inputs["dry_run"]
            and previous.get("snapshot_key") == key
            and previous["status"] in {"running", "stopped"}
        )
        _stop(folder)
        reusable = reusable and all(
            (directory / worker / "vm.json").is_file()
            and read(directory / worker / "vm.json").get("snapshot") == SNAPSHOT_NAME
            and _has_snapshot(directory / worker)
            for worker in plans
        )
        if reusable:
            report = _restore(directory, previous)
        else:
            shutil.rmtree(directory)
            report = _launch(folder, previous["task"], **inputs)
        report["reset_mode"] = "snapshot" if reusable else "fresh_boot"
        report["reset_seconds"] = time.monotonic() - started
        write(directory / "LAUNCH.json", report)
        return report
