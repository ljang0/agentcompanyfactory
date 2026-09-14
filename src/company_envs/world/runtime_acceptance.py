"""Repeatable browser/write/access/resource/reset evidence for a populated company."""

import json
import time
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from company_envs.config import load_config
from company_envs.preflight import EnvironmentBlocked
from company_envs.storage import digest, now, read, write

from .hub_access import role
from .hub_app import HubClient, source_hash
from .hub_identity import visible_to
from .hub_mail import canonical_mail
from .hub_world import CompanyWorld
from .interact_check import plan_probe, screen_error, shows_something, storage_overflow, write_reached_state
from .population_quality import DOC_MIME, instant, plain
from .population_snapshot import population_snapshot


def implementation_snapshot(root):
    root = Path(root)
    paths = sorted((root / "src/company_envs/world").glob("hub*"))
    paths += [
        root / "src/company_envs/world" / name
        for name in (
            "runtime_acceptance.py",
            "world_acceptance.py",
            "population_snapshot.py",
            "interact_check.py",
            "app_audit.py",
        )
    ]
    paths += [
        root / "src/company_envs/preflight.py",
        root / "catalogs/runtime_probes.json",
        root / "catalogs/app_entry_routes.json",
    ]
    return {str(p.relative_to(root)): digest(p.read_bytes()) for p in paths if p.is_file()}


def origin(url):
    return urlsplit(url)._replace(path="", query="", fragment="").geturl()


def prepared_probe(root, app_id, state, person, marker):
    plans = read(Path(root) / "catalogs/runtime_probes.json")
    if app_id not in plans:
        raise ValueError(f"No reviewed browser write probe for {app_id}")
    plan = plans[app_id]
    text = json.dumps(plan).replace(plan["marker"], marker)
    if "{document_id}" in text:
        rid = next(
            (rid for rid, doc in state["documents"].items() if role(doc, person) in {"owner", "editor"}), None
        )
        if rid is None:
            raise ValueError("No editable document for the browser probe")
        text = text.replace("{document_id}", rid)
    if "{campaign_id}" in text:
        text = text.replace("{campaign_id}", state["campaigns"][0]["id"])
    return json.loads(text)


def _access(world):
    """Read every granted view and attempt hidden/read-only record edits over its proxy."""
    rows = []
    grants = read(world.folder / "world/worker_apps.json")
    actual = {(a, w) for a, e in world.endpoints.items() for w in e["workers"]}
    expected = {(a, w) for w, apps in grants.items() for a in apps}
    if actual != expected:
        raise ValueError("Served worker apps differ from the recorded grants")
    for item in world.plan:
        aid = item["app_id"]
        for worker, url in world.endpoints[aid]["workers"].items():
            person = item["users"][worker]
            shared = world.clients[aid].current(world.sid)["stored_state"]
            client = HubClient(origin(url), timeout=30)
            delivered = world.resources.rewrite(client.current(world.sid)["stored_state"], reverse=True)
            delivered = canonical_mail(delivered) if aid == "gmail_mock" else delivered
            expected_view = visible_to(shared, person)
            checked, denied = [], []
            for key in (
                "emails",
                "drafts",
                "events",
                "documents",
                "items",
                "comments",
                "messages",
                "threads",
                "dms",
                "channels",
            ):
                if key in expected_view:
                    if delivered.get(key) != expected_view[key]:
                        raise ValueError(f"{aid}/{worker}: scoped {key} differs")
                    checked.append(key)
            for key in ("documents", "items"):
                if not isinstance(shared.get(key), dict):
                    continue
                targets = [
                    (rid, doc)
                    for rid, doc in shared[key].items()
                    if role(doc, person) in {None, "viewer", "commenter"}
                ]
                # One actual forbidden record per view is enough to exercise the server gate.
                if targets:
                    rid, doc = targets[0]
                    bad = deepcopy(delivered)
                    bad[key][rid] = {**doc, "content": "Unauthorized acceptance probe"}
                    before = digest(shared)
                    try:
                        client.update(world.sid, bad)
                    except HTTPError as exc:
                        if exc.code != 403:
                            raise
                    else:
                        raise ValueError(f"{aid}/{worker}: forbidden edit was accepted")
                    if digest(world.clients[aid].current(world.sid)["stored_state"]) != before:
                        raise ValueError("Refused write changed shared state")
                    denied.append(f"{key}/{rid}")
            rows.append(
                {
                    "app": aid,
                    "worker": worker,
                    "scoped_collections": checked,
                    "forbidden_edits_rejected": denied,
                    "ok": True,
                }
            )
    return rows


def _resources(world):
    """Fetch representatives of each actual file format, plus every campaign and ZIP."""
    rows, chosen, formats = [], set(), set()
    for route, resource in world.resources.resources.items():
        path = resource["path"]
        suffix = path.suffix.lower()
        if resource["sha256"] in chosen:
            continue
        if suffix not in formats or suffix in {".zip", ".svg", ".png"}:
            chosen.add(resource["sha256"])
            formats.add(suffix)
            found = False
            for item in world.plan:
                for worker, url in world.endpoints[item["app_id"]]["workers"].items():
                    view = visible_to(
                        world.clients[item["app_id"]].current(world.sid)["stored_state"],
                        item["users"][worker],
                    )
                    if not world.resources.permitted(route, view):
                        continue
                    response = requests.get(origin(url) + route, timeout=30)
                    if response.status_code != 200 or digest(response.content) != resource["sha256"]:
                        raise ValueError(f"Resource readback failed: {path.name}")
                    rows.append(
                        {
                            "file": str(path.relative_to(world.folder)),
                            "app": item["app_id"],
                            "worker": worker,
                            "bytes": len(response.content),
                            "sha256": digest(response.content),
                            "ok": True,
                        }
                    )
                    found = True
                    break
                if found:
                    break
            if not found:
                raise ValueError(f"No worker can retrieve advertised resource {path.name}")
    if world.resources.resources and not rows:
        raise ValueError("Resource probes did not run")
    return rows


def _shared_edits(world):
    """Exercise two stale worker views and the live Docs/Drive projection over real proxies."""
    item = next((i for i in world.plan if i["app_id"] == "google_docs_mock"), None)
    if not item or "google_drive_mock" not in world.clients:
        return {"applicable": False}
    drive = world.clients["google_drive_mock"].current(world.sid)["stored_state"]
    for rid, document in item["state"]["documents"].items():
        workers = [w for w in item["workers"] if role(document, item["users"][w]) in {"owner", "editor"}]
        if len(workers) > 1 and drive["items"].get(rid, {}).get("mimeType") == DOC_MIME:
            break
    else:
        return {"applicable": False, "reason": "No native document grants two workers edit access"}
    workers = workers[:2]
    clients = [HubClient(origin(world.endpoints[item["app_id"]]["workers"][w]), timeout=30) for w in workers]
    views = [c.current(world.sid)["stored_state"] for c in clients]
    title = views[0]["documents"][rid]["title"] + " — shared edit"
    content = views[1]["documents"][rid]["content"] + "<p>Acceptance shared edit readback.</p>"
    views[0]["documents"][rid]["title"] = title
    views[1]["documents"][rid]["content"] = content
    for client, view in zip(clients, views, strict=True):
        client.update(world.sid, view)
    doc = world.clients[item["app_id"]].current(world.sid)["stored_state"]["documents"][rid]
    file = world.clients["google_drive_mock"].current(world.sid)["stored_state"]["items"][rid]
    if (
        doc["title"] != title
        or doc["content"] != content
        or file["name"] != title
        or file["content"] != plain(content)
    ):
        raise ValueError("Independent worker edits were lost or the live Drive projection diverged")
    return {
        "applicable": True,
        "ok": True,
        "record": rid,
        "workers": workers,
        "fields": ["title", "content"],
        "drive_hash": digest(file),
    }


def _actor_evidence(world, out, writes):
    rows = {}
    for item in world.plan:
        aid = item["app_id"]
        path = world.folder / "runtime/attribution" / f"{aid}.jsonl"
        rows[aid] = [
            r for line in path.read_text().splitlines() if (r := json.loads(line))["sid"] == world.sid
        ]
        write(out / f"actors-{aid}.json", rows[aid])
    for report in writes:
        if not any(
            r["worker_id"] == report["worker"] and report["collection"] in r["changed_keys"]
            for r in rows[report["app"]]
        ):
            raise ValueError(f"Missing trusted actor evidence for {report['app']} write")
    return {aid: len(r) for aid, r in rows.items()}


def runtime_timezone(folder):
    """Use a supplied company zone; UTC is the explicit fallback, never a guessed location."""
    folder = Path(folder)
    manifest = read(folder / "world/POPULATION.json")
    zone = read(folder / manifest["effective_world"]).get("timezone", "UTC")
    ZoneInfo(zone)  # Invalid declarations must fail before starting a browser.
    return zone


def _browser_downloads(world, browser, out, clock, timezone):
    aid = "google_drive_mock"
    if aid not in world.endpoints:
        return []
    item = next(i for i in world.plan if i["app_id"] == aid)
    rows = []
    for suffix in (".zip", ".pdf"):
        candidates = [
            (route, r)
            for route, r in world.resources.resources.items()
            if r["path"].suffix.lower() == suffix and r["id"] in item["state"].get("items", {})
        ]
        if not candidates:
            continue
        _route, resource = candidates[0]
        file = item["state"]["items"][resource["id"]]
        worker = next(w for w in item["workers"] if role(file, item["users"][w]))
        context = browser.new_context(
            accept_downloads=True, viewport={"width": 1440, "height": 1000}, timezone_id=timezone
        )
        try:
            page = context.new_page()
            page.clock.set_fixed_time(clock)
            page.set_default_timeout(8000)
            page.goto(world.endpoints[aid]["workers"][worker], wait_until="domcontentloaded")
            page.get_by_placeholder("Search in Drive").fill(file["name"])
            page.get_by_placeholder("Search in Drive").press("Enter")
            page.get_by_text(file["name"], exact=True).first.dblclick()
            page.wait_for_timeout(500)
            page.screenshot(path=str(out / f"download-{suffix[1:]}.png"))
            if suffix == ".pdf":
                source = page.locator("iframe").get_attribute("src")
                if not source or "/__company_resource/" not in source:
                    raise ValueError("PDF preview did not load the real resource")
            with page.expect_download(timeout=15000) as pending:
                page.get_by_role("button", name="Download", exact=True).click()
            download = pending.value
            target = out / ("download" + suffix)
            download.save_as(target)
            if digest(target.read_bytes()) != resource["sha256"]:
                raise ValueError("Browser download differs from the resource manifest")
            rows.append(
                {
                    "worker": worker,
                    "record": resource["id"],
                    "name": file["name"],
                    "sha256": resource["sha256"],
                    "file": target.name,
                    "ok": True,
                }
            )
        finally:
            context.close()
    return rows


def verify_runtime(root, folder, *, work=None):
    """Own the app lifecycle, gather evidence, reset, and stop; never invoke a model."""
    from playwright.sync_api import sync_playwright

    root, folder = Path(root).resolve(), Path(folder).resolve()
    config = load_config(root)
    baseline, implementation = population_snapshot(root, folder), implementation_snapshot(root)
    run_id = now().replace(":", "-")
    out = folder / "world/acceptance/runtime" / run_id
    out.mkdir(parents=True)
    result = {
        "schema_version": 1,
        "at": now(),
        "baseline": baseline,
        "implementation": implementation,
        "checks": {},
        "evidence": {},
    }
    world = CompanyWorld(
        folder,
        Path(config["design"]["hub_root"]),
        work or folder / "runtime/hub-cache",
        episode="acceptance-" + digest(run_id)[:10],
        host="127.0.0.1",
        root=root,
        log_dir=out / "logs",
    )
    started = time.monotonic()
    try:
        world.start()
        builds = {}
        for item in world.plan:
            inspected = world.clients[item["app_id"]].inspect(world.sid)
            if inspected["initial_state"] != item["state"] or inspected["current_state"] != item["state"]:
                raise ValueError("Build seed/readback mismatch")
            aid = item["app_id"]
            marker = read(Path(world.work) / aid / ".hub-build.json")
            marker.pop("source", None)
            builds[aid] = {
                "seed_hash": digest(item["state"]),
                "native_source_hash": source_hash(world.hub_root / aid),
                "build": marker,
                "ok": True,
            }
        result["builds"] = builds
        result["access"] = _access(world)
        print(f"Built {len(builds)} apps; verified {len(result['access'])} worker views", flush=True)
        browser_path = Path(config["design"]["vm_browser_dir"]) / "chrome"
        clock = instant(read(folder / "world/POPULATION.json")["reference_date"])
        timezone = runtime_timezone(folder)
        result["clock"] = {"instant": clock.isoformat(), "timezone": timezone}
        rendered, writes = [], []
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=str(browser_path), headless=True, args=["--no-sandbox"]
            )
            try:
                for item in world.plan:
                    aid = item["app_id"]
                    for index, (worker, url) in enumerate(world.endpoints[aid]["workers"].items()):
                        context = browser.new_context(
                            viewport={"width": 1440, "height": 1000}, timezone_id=timezone
                        )
                        try:
                            page = context.new_page()
                            page.clock.set_fixed_time(clock)
                            page.set_default_timeout(6000)
                            errors = []
                            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                            t = time.monotonic()
                            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(900)
                            body = page.locator("body").inner_text()
                            ok = (
                                response.status == 200
                                and not errors
                                and shows_something(page, body)
                                and not screen_error(aid, body)
                                and not storage_overflow(page)
                            )
                            page.screenshot(path=str(out / f"render-{aid}-{worker}.png"))
                            rendered.append(
                                {
                                    "app": aid,
                                    "worker": worker,
                                    "ok": bool(ok),
                                    "seconds": round(time.monotonic() - t, 3),
                                    "text_chars": len(body),
                                    "excerpt": body[:800],
                                    "page_errors": list(errors),
                                }
                            )
                            if not ok:
                                raise ValueError(f"Browser render failed: {aid}/{worker}")
                            if index == 0:
                                client = world.clients[aid]
                                before = client.current(world.sid)["stored_state"]
                                plan = prepared_probe(
                                    root,
                                    aid,
                                    before,
                                    item["users"][worker],
                                    "Acceptance " + digest(run_id + aid)[:12],
                                )
                                report = plan_probe(
                                    page,
                                    origin(url),
                                    world.sid,
                                    plan,
                                    before,
                                    state_of=lambda client=client: client.current(world.sid)["stored_state"],
                                )
                                after = client.current(world.sid)["stored_state"]
                                reached = write_reached_state(report, after, before)
                                report.update(
                                    app=aid,
                                    worker=worker,
                                    before_hash=digest(before),
                                    after_hash=digest(after),
                                    page_errors=list(errors),
                                )
                                report["ok"] = bool(
                                    reached
                                    and report.get("marker_in_collection")
                                    and report.get("marker_seen_before_reload")
                                    and not any(
                                        report.get(k) for k in ("step_error", "reload_error", "page_errors")
                                    )
                                )
                                writes.append(report)
                                page.screenshot(path=str(out / f"write-{aid}.png"))
                                if not report["ok"]:
                                    raise ValueError(f"Browser write failed: {aid}: {report}")
                        finally:
                            context.close()
                    print(f"Browser render and write passed: {aid}", flush=True)
                    write(out / "BROWSER.json", {"rendered": rendered, "writes": writes})
                result["downloads"] = _browser_downloads(world, browser, out, clock, timezone)
            finally:
                browser.close()
        result["rendered"], result["writes"] = rendered, writes
        result["resources"] = _resources(world)
        result["shared_edits"] = _shared_edits(world)
        result["actors"] = _actor_evidence(world, out, writes)
        # The source seed is never replaced by an ordinary worker save.
        for item in world.plan:
            if world.clients[item["app_id"]].inspect(world.sid)["initial_state"] != item["state"]:
                raise ValueError("A browser save altered the reset baseline")
        world.reset()
        result["reset"] = {}
        for item in world.plan:
            inspected = world.clients[item["app_id"]].inspect(world.sid)
            ok = (
                inspected["initial_state"] == item["state"] == inspected["current_state"]
                and not inspected["state_diff"]
            )
            result["reset"][item["app_id"]] = {
                "ok": bool(ok),
                "state_hash": digest(inspected["current_state"]),
            }
            if not ok:
                raise ValueError(f"Reset failed: {item['app_id']}")
        if any(proxy.base is not None or proxy.authored or proxy._view_bases for proxy in world.proxies):
            raise ValueError("Reset did not clear proxy session caches")
        for item in world.plan:
            path = folder / "runtime/attribution" / f"{item['app_id']}.jsonl"
            if any(json.loads(line).get("sid") == world.sid for line in path.read_text().splitlines()):
                raise ValueError("Reset retained earlier trial contributions")
        if population_snapshot(root, folder) != baseline or implementation_snapshot(root) != implementation:
            raise ValueError("Acceptance inputs changed while testing")
        result["checks"] = dict.fromkeys(
            ("build", "render", "writes", "persistence", "access", "resources", "reset"), True
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, EnvironmentBlocked):
            result["preflight"] = exc.report
        result["status"] = (
            "environment_blocked"
            if isinstance(exc, (PermissionError, EnvironmentBlocked))
            else "verification_failed"
        )
        raise
    finally:
        world.stop()
        result["seconds"] = round(time.monotonic() - started, 3)
        result["evidence"] = {
            str(path.relative_to(folder)): digest(path.read_bytes())
            for path in sorted(out.rglob("*"))
            if path.is_file()
        }
        write(out / "RESULT.json", result)
        write(folder / "world/acceptance/RUNTIME.json", result)
    return result
