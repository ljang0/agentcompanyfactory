"""Bring one company folder's seeded hub apps up as worker-facing services.

Reads ``companies/<id>/apps.json`` and ``world/<app_id>.state.json``, builds and serves
each hub app on loopback, seeds one shared session per app, fronts each with a
``WorkerProxy`` bound on the VM-reachable host, and writes ``runtime/sessions.json`` and
``runtime/endpoints.json``. Workers get only the proxy URLs. This is the Stage 2 to
Stage 3 handoff for hub apps; it is not a VM launcher or a grader.
"""

import json
import re
import signal
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from company_envs.config import load_config
from company_envs.preflight import EnvironmentBlocked, require_runtime_environment
from company_envs.storage import digest, now, read, write

from .hub_app import HubClient, HubProcess, build, entry_routes, validate_sid, validate_state
from .hub_documents import project_document_edits
from .hub_identity import (
    IDENTITY_MARKERS,
    WorkerAppProxy,
    _json_equal,
    _records,
    identity_markers,
    is_pointer_identity,
    pointer_identity,
)
from .hub_resources import ResourceStore
from .state_seed import canonical_people
from .world_check import has_user_collection

__all__ = [
    "IDENTITY_MARKERS",
    "CompanyWorld",
    "appears_in_state",
    "entry_path",
    "identity_view",
    "load_world",
    "serve_company",
]


def placeholder_images_enabled(root):
    """The ``[design] placeholder_images`` flag: on unless a readable config turns it off."""
    try:
        config = load_config(root)
    except (OSError, ValueError):
        return True
    design = config.get("design")
    return bool((design if isinstance(design, dict) else {}).get("placeholder_images", True))


# What ``hub-serve`` hands ``launch-vms``: true only while the serving process is alive.
HANDOFF_FILES = ("endpoints.json", "sessions.json")
# A route on the app's own origin: no scheme, host, query or fragment, since the session id is
# appended to the worker URL separately.
_ENTRY_PATH = re.compile(r"/[A-Za-z0-9._~\-/]*")


def entry_path(entry, route=None):
    """The route a worker's URL opens on: where this app keeps the company's records.

    ``/`` is a launcher or a dashboard in several clones rather than the world. Measured on the
    currently passing renders: google_drive on ``/`` shows 1 of 400 seeded values in ~210 characters
    against 5-26 in 1.3-2.0k on ``/recent``; google_sheets' ``/`` is a "Recent spreadsheets" launcher
    at ~180 characters against up to 15k on ``/spreadsheet``; hubspot's ``/contacts`` goes from 2 of
    400 to 24-31. One route per app, because the render gate, the VM launcher and the worker's start
    page all have to open the same page -- and this URL is the one string ``hub_vm`` turns into the
    guest's bookmark, its start-page tile and its startup tab.

    The fact lives upstream in ``catalogs/app_entry_routes.json``, read through
    :func:`hub_app.entry_routes` so the gate and the launcher have one reader; a company's own
    ``apps.json`` entry may override it. No table lives here.
    """
    for value in ((entry or {}).get("entry_path"), route):
        if isinstance(value, str) and _ENTRY_PATH.fullmatch(value):
            return value
    return "/"


def episode_sid(company_id, episode):
    sid = f"{company_id}-{episode}".replace("_", "-")
    return validate_sid("".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in sid)[:64])


def load_world(folder, root=None):
    """Validate the folder's app states against their schemas before touching any server."""
    folder = Path(folder)
    root = Path(root) if root else folder.parents[1]
    manifest = read(folder / "apps.json")
    apps = manifest["apps"]
    workers = manifest.get("workers") or []
    identities_path = folder / manifest.get("identities_file", "world/identities.json")
    identities = read(identities_path) if identities_path.is_file() else {}
    if (folder / "world/CORE.json").exists():
        from .population_snapshot import population_snapshot

        population_snapshot(root, folder)
    grants_path = folder / "world/worker_apps.json"
    grants = read(grants_path) if grants_path.exists() else {w: [a["app_id"] for a in apps] for w in workers}
    world_path = folder / "world/population/EFFECTIVE-WORLD.json"
    canonical_people_by_worker = canonical_people(read(world_path), workers) if world_path.exists() else {}
    routes = entry_routes()
    plan = []
    missing = []
    for entry in apps:
        state_path = folder / entry["state_file"]
        if not entry.get("hub_seedable"):
            missing.append({"app_id": entry["app_id"], "reason": "not a hub app with a schema"})
            continue
        if not state_path.is_file():
            missing.append({"app_id": entry["app_id"], "reason": f"missing {entry['state_file']}"})
            continue
        schema_text = (root / entry["schema"]).read_text() if entry.get("schema") else None
        state = read(state_path)
        validate_state(
            {"id": entry["app_id"], "state_keys": entry.get("top_level_keys") or []}, state, schema_text
        )
        key = entry.get("identity_key")
        allowed_workers = [w for w in workers if entry["app_id"] in grants.get(w, [])]
        users = {w: canonical_people_by_worker.get(w) for w in allowed_workers}
        if key:
            records = {w: (identities.get(w) or {}).get(entry["app_id"]) for w in allowed_workers}
            directory = has_user_collection(state, [r for r in records.values() if isinstance(r, dict)])
            for worker, record in records.items():
                if not isinstance(record, dict):
                    missing.append({"app_id": entry["app_id"], "reason": f"no identity for worker {worker}"})
                elif directory and not appears_in_state(record, state):
                    # A single-account app (a mailbox, a calendar) has no directory to hold the
                    # worker; the proxy supplies the identity, the same exemption the seeding gate uses.
                    missing.append(
                        {
                            "app_id": entry["app_id"],
                            "reason": f"identity for {worker} not in a state collection",
                        }
                    )
                else:
                    users[worker] = record
        canonical = state.get(key) if key else None
        plan.append(
            {
                "app_id": entry["app_id"],
                "state": state,
                "state_file": entry["state_file"],
                "entry_path": entry_path(entry, routes.get(entry["app_id"])),
                "identity_key": key,
                "users": users,
                "workers": allowed_workers,
                "canonical_user": canonical,
                # An identity key that holds an id, not a record: the proxy serves this worker's own
                # id there. Without it the four pointer apps were served with no injection at all.
                "pointers": (
                    {w: pointer_identity(state, key, r) for w, r in users.items()}
                    if key and is_pointer_identity(canonical)
                    else {}
                ),
            }
        )
    if missing:
        raise ValueError(f"World is incomplete for hub serving: {missing}")
    return plan


def appears_in_state(record, state):
    """A worker's user record must be a real member of some collection in the seeded state.

    Membership is matched on any of ``IDENTITY_MARKERS``, per field. Recognising only ``id`` and
    ``email`` meant zhihu_mock -- whose user records carry neither, only ``userId`` -- could never
    satisfy this gate, and load_world refused to serve any company world holding it.
    """
    markers = identity_markers(record)
    if not markers:
        return False
    for value in state.values():
        for item in _records(value):
            for field, marker in markers:
                other = item.get(field)
                if isinstance(other, (str, int)) and str(other).strip() == marker:
                    return True
    return False


def identity_view(item, worker=None):
    """What the worker path serves at this app's identity key, and whether the seed differs.

    Two paths read every seeded world and they do not see the same thing. A host-side reader -- a
    grader, a render or visual gate, a driver -- reads the raw state file. A worker reads it through
    its proxy, which replaces the identity key with that worker's own record (or id). Where the
    seeded value is not of the same shape, the proxy *repairs* a defect the host reads as a failure:
    mailchimp seeds ``user`` as a list and its Layout does ``state.user.firstName[0]``, so the host
    crashes on the raw world and a worker VM renders it; hubspot, instagram and lattice likewise
    showed seeded records in a guest where the host said they did not. Both readings are true of
    their own path, so a verdict has to say which path it is about -- this is that statement,
    written into the handoff beside the URLs.
    """
    key = item.get("identity_key")
    if not key:
        return {"key": None, "serves": "seed", "repairs_seed": False}
    canonical = item.get("canonical_user")
    pointers = item.get("pointers") or {}
    pointer = pointers.get(worker) if worker else None
    return {
        "key": key,
        "serves": "worker_id" if pointers else "worker_record",
        "seeded_type": type(canonical).__name__,
        # The seed holds something the app cannot read where the proxy holds something it can.
        "repairs_seed": not (is_pointer_identity(canonical) if pointers else isinstance(canonical, dict)),
        **({"worker_value": pointer} if pointer is not None else {}),
    }


class CompanyWorld:
    """Running seeded apps for one company episode; stop() tears everything down."""

    def __init__(self, folder, hub_root, work, *, episode="ep1", host="0.0.0.0", root=None, log_dir=None):
        self.folder = Path(folder)
        self.hub_root = Path(hub_root)
        self.work = Path(work)
        self.host = host
        self.root = Path(root) if root else self.folder.parents[1]
        self.company_id = read(self.folder / "MANIFEST.json")["company_id"]
        self.sid = episode_sid(self.company_id, episode)
        self.log_dir = Path(log_dir) if log_dir else self.folder / "runtime" / "logs"
        self.plan = load_world(self.folder, self.root)
        self.workers = read(self.folder / "apps.json").get("workers") or []
        self.placeholder_images = (
            False
            if (self.folder / "world/POPULATION.json").exists()
            else placeholder_images_enabled(self.root)
        )
        self.resources = ResourceStore(self.folder, {item["app_id"]: item["state"] for item in self.plan})
        self.processes = []
        self.proxies = []
        self.endpoints = {}
        self.clients = {}
        self.document_lock = threading.RLock()

    def drop_handoff(self):
        """Remove the Stage 2 -> Stage 3 handoff files, true only while this process runs.

        ``launch-vms`` reads runtime/endpoints.json for the proxy port of every app. The ports are
        this process's, so a file left behind by an earlier run (a ``hub-serve --check``, a crash)
        hands the launcher dead ports -- and the guest's readiness check then fails with a
        connection *reset* rather than refused, because QEMU's guestfwd always accepts the TCP
        connection before its host-side ``nc`` can fail. One VM run was spent reading that as a
        broken app. Deleting the files before serving means a launcher either finds a live handoff
        or finds none.
        """
        removed = []
        for name in HANDOFF_FILES:
            path = self.folder / "runtime" / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        return removed

    def start(self, server_factory=None, proxy_factory=None):
        """Build, serve, seed and front every app; factories exist for offline tests."""
        if server_factory is None or proxy_factory is None:
            try:
                require_runtime_environment()
            except EnvironmentBlocked as exc:
                write(self.folder / "runtime/preflight" / f"{time.time_ns()}.json", exc.report)
                raise
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.drop_handoff()
        self.archive_attribution()
        server_factory = server_factory or (
            lambda app_id: HubProcess(
                build(self.hub_root / app_id, self.work / app_id, log=self.log_dir / f"{app_id}-build.log"),
                log=self.log_dir / f"{app_id}-serve.log",
            ).start()
        )
        proxy_factory = proxy_factory or (
            lambda upstream, item, worker: WorkerAppProxy(
                upstream,
                worker_id=worker,
                sid=self.sid,
                identity_key=item["identity_key"],
                user_record=item["users"].get(worker),
                user_pointer=item.get("pointers", {}).get(worker),
                canonical_user=item["canonical_user"],
                attribution_log=self.folder / "runtime" / "attribution" / f"{item['app_id']}.jsonl",
                host=self.host,
                placeholder_images=self.placeholder_images,
                resources=self.resources,
                app_id=item["app_id"],
                on_write=lambda worker_id, previous, state: self.project_write(
                    item["app_id"], worker_id, previous, state
                ),
            ).start()
        )
        try:
            for item in self.plan:
                server = server_factory(item["app_id"])
                self.processes.append(server)
                client = server.client if hasattr(server, "client") else HubClient(server.base_url)
                self.clients[item["app_id"]] = client
                client.seed(self.sid, item["state"])
                seen = client.inspect(self.sid)
                if (
                    not _json_equal(seen.get("initial_state"), item["state"])
                    or not _json_equal(seen.get("current_state"), item["state"])
                    or seen.get("state_diff") != {}
                ):
                    raise ValueError(f"{item['app_id']} did not report the seeded state back")
                per_worker = {}
                for worker in item.get("workers", self.workers) or (["shared"] if not self.workers else []):
                    proxy = proxy_factory(client.base_url, item, worker)
                    if item["app_id"] in {"google_docs_mock", "google_drive_mock"}:
                        proxy._merge_lock = self.document_lock
                    self.proxies.append(proxy)
                    per_worker[worker] = f"http://{self.host}:{proxy.port}{item['entry_path']}?sid={self.sid}"
                self.endpoints[item["app_id"]] = {
                    "harness_url": client.base_url,
                    "state_file": item["state_file"],
                    "identity_key": item["identity_key"],
                    "identity": identity_view(item),
                    # The route the worker URLs carry, so the render gate judges the same page.
                    "entry_path": item["entry_path"],
                    "workers": per_worker,
                }
        except Exception:
            self.stop()
            raise
        runtime = self.folder / "runtime"
        write(
            runtime / "sessions.json",
            {
                "identity": "per_worker_proxy",
                "sid": self.sid,
                "workers": self.workers,
                "started_at": now(),
                "apps": sorted(self.endpoints),
                # Apps whose worker path shows something the raw state file does not: a host-side
                # verdict about one of these is a verdict about a state no worker ever sees.
                "identity_repairs_seed": sorted(
                    app for app, entry in self.endpoints.items() if entry["identity"]["repairs_seed"]
                ),
            },
        )
        write(
            runtime / "endpoints.json",
            {
                "note": "Each worker VM receives only its own URLs; harness_url stays on the host.",
                "apps": self.endpoints,
            },
        )
        return self

    def project_write(self, app_id, worker_id, previous, state):
        peer_id = {"google_docs_mock": "google_drive_mock", "google_drive_mock": "google_docs_mock"}.get(
            app_id
        )
        if peer_id not in self.clients:
            return
        client = self.clients[peer_id]
        before = client.current(self.sid)["stored_state"]
        after = project_document_edits(app_id, previous, state, before)
        if after != before:
            try:
                client.update(self.sid, after)
            except Exception:
                self.clients[app_id].update(self.sid, previous)
                client.update(self.sid, before)
                raise
            path = self.folder / "runtime/attribution" / f"{peer_id}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "at": now(),
                            "worker_id": worker_id,
                            "sid": self.sid,
                            "projection_of": app_id,
                            "changed_keys": [k for k in after if after[k] != before.get(k)],
                            "conflicts": [],
                        }
                    )
                    + "\n"
                )

    def reset(self):
        """Reset state, proxy caches and episode attribution; callers close worker browsers first."""
        locks = {id(p._merge_lock): p._merge_lock for p in self.proxies if hasattr(p, "_merge_lock")}
        with ExitStack() as stack:
            for _, lock in sorted(locks.items()):
                stack.enter_context(lock)
            for server, item in zip(self.processes, self.plan, strict=True):
                client = server.client if hasattr(server, "client") else HubClient(server.base_url)
                client.reset(self.sid)
                client.seed(self.sid, item["state"])
            for proxy in self.proxies:
                proxy.base = None
                proxy._view_bases.clear()
                proxy.authored = set()
            self.archive_attribution()

    def archive_attribution(self):
        """A previous trial's writes must never count as this trial's contributions."""
        directory = self.folder / "runtime/attribution"
        archive = directory / "archive" / (self.sid + "-" + digest(now())[:12])
        for path in sorted(directory.glob("*.jsonl")):
            selected, kept = [], []
            for line in path.read_text().splitlines(keepends=True):
                try:
                    entry = json.loads(line)
                except ValueError:
                    kept.append(line)
                    continue
                (selected if entry.get("sid") == self.sid else kept).append(line)
            if selected:
                archive.mkdir(parents=True, exist_ok=True)
                (archive / path.name).write_text("".join(selected))
                path.write_text("".join(kept))

    def stop(self):
        for proxy in self.proxies:
            proxy.stop()
        for server in self.processes:
            server.stop()
        self.proxies, self.processes = [], []
        # Nothing is listening on those ports any more, so the handoff is no longer true.
        self.drop_handoff()

    def wait(self):
        """Block until SIGINT/SIGTERM, then stop."""
        done = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: done.set())
        while not done.is_set():
            time.sleep(0.5)
        self.stop()


def serve_company(folder, hub_root, work, *, episode="ep1", host="0.0.0.0", check_only=False, root=None):
    world = CompanyWorld(folder, hub_root, work, episode=episode, host=host, root=root).start()
    report = {"company_id": world.company_id, "sid": world.sid, "endpoints": world.endpoints}
    if check_only:
        world.stop()
        return report
    print(json.dumps(report, indent=2), flush=True)
    world.wait()
    return report
