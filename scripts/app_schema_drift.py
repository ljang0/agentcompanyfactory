#!/usr/bin/env python3
"""Compare each app's pinned schema against the state the app itself actually keeps.

Seeding is driven by the ``## State Schema`` table in ``catalogs/app_schemas/<app>.md``:
``hub_app.top_level_keys`` reads it, and ``validate_state`` refuses any key it does not list.
So a key the app renders but the table omits can never be seeded -- the app falls back to the
demo records it ships with, and those then get written into the company's world. A key the
table lists but the app does not keep is the opposite waste: seeded records nothing reads.

Neither kind of drift is visible from the document alone, so this asks the app. Each app is
served unseeded and opened in a browser; whatever it then holds -- on the server if it posts
its own defaults, in localStorage otherwise -- is its real state, and its top-level keys are
compared with the table's.
"""

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from company_envs.storage import write
from company_envs.world.hub_app import HubProcess, build, hub_apps, top_level_keys

# The shape of a value, named the way a schema table names it, plus the fields of the first
# record, so the table can say what a record holds and not only that a collection exists.
SHAPE = """(value) => {
  const kind = (v) => Array.isArray(v) ? 'array' : v === null ? 'null' : typeof v;
  const first = Array.isArray(value) ? value[0]
    : (value && typeof value === 'object' ? Object.values(value)[0] : null);
  return {
    kind: kind(value),
    count: Array.isArray(value) ? value.length
      : (value && typeof value === 'object' ? Object.keys(value).length : null),
    record_fields: first && typeof first === 'object' && !Array.isArray(first)
      ? Object.keys(first).slice(0, 40) : null,
    keyed_by_id: !Array.isArray(value) && value && typeof value === 'object'
      && Object.keys(value).length > 0
      && Object.values(value).every(v => v && typeof v === 'object' && !Array.isArray(v)),
  };
}"""

LARGEST_JSON = """() => {
  let best = null;
  for (let i = 0; i < localStorage.length; i++) {
    const name = localStorage.key(i);
    const raw = localStorage.getItem(name) || '';
    if (raw.length < 2 || !'{['.includes(raw[0])) continue;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        if (!best || raw.length > best.size) best = {name, size: raw.length, value: parsed};
      }
    } catch (error) { /* not this one */ }
  }
  return best;
}"""


def observed_state(app_id, source, work, browser, log_dir):
    """The state the app keeps when nobody seeded it: the server's copy, else the browser's."""
    built = build(source, work / app_id, log=log_dir / f"{app_id}-build.log")
    with HubProcess(built, log=log_dir / f"{app_id}-serve.log") as server:
        page = browser.new_context(viewport={"width": 1400, "height": 900}).new_page()
        page.goto(f"{server.client.base_url}/?sid=drift", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        local = page.evaluate(LARGEST_JSON)
        state, where = None, "nothing_observed"
        posted = server.client.inspect("drift").get("current_state") or {}
        if isinstance(posted, dict) and posted:
            state, where = posted, "posted_to_server"
        elif local and isinstance(local.get("value"), dict):
            state, where = local["value"], f"localStorage:{local['name']}"
        shapes = {key: page.evaluate(SHAPE, value) for key, value in (state or {}).items()}
        page.context.close()
    return sorted(state or {}), where, shapes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="*")
    parser.add_argument("--work", type=Path, default=ROOT / "experiments/hub-cache")
    parser.add_argument("--hub-root", type=Path)
    parser.add_argument("--schemas", type=Path, default=ROOT / "catalogs/app_schemas")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    catalog = hub_apps(json.loads((ROOT / "catalogs/apps.json").read_text())["apps"])
    apps = args.apps or sorted(catalog)
    hub_root = args.hub_root or Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"])
    (args.out / "logs").mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch(args=["--no-sandbox"])
        try:
            for app_id in apps:
                schema = args.schemas / f"{app_id}.md"
                documented = sorted(top_level_keys(schema.read_text()) or []) if schema.is_file() else []
                try:
                    observed, where, shapes = observed_state(
                        app_id, hub_root / app_id, args.work, browser, args.out / "logs"
                    )
                except Exception as exc:  # noqa: BLE001 -- an app that will not serve is the finding
                    observed, where, shapes = [], f"error: {type(exc).__name__}: {exc}"[:300], {}
                undocumented = [key for key in observed if key not in documented]
                unused = [key for key in documented if key not in observed]
                report = {
                    "app_id": app_id,
                    "source": where,
                    "documented": documented,
                    "observed": observed,
                    "undocumented_so_unseedable": undocumented,
                    "documented_but_app_keeps_none": unused,
                    "agrees": not undocumented and not unused and bool(observed),
                    "shapes": shapes,
                }
                write(args.out / f"{app_id}.json", report)
                print(
                    f"{'OK  ' if report['agrees'] else 'DRIFT'} {app_id:<28}"
                    f" doc={len(documented):<3} app={len(observed):<3}"
                    f" unseedable={','.join(undocumented)[:60]:<60} unused={','.join(unused)[:40]}",
                    flush=True,
                )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
