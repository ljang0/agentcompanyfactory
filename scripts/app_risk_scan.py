#!/usr/bin/env python3
"""Scan every hub app's source for the defect classes live VMs found in a few of them.

Each class below was measured in a guest on at least one app and then cost real time to explain.
None of them is visible from the host, from a screenshot, or from the seeded data; all of them are
greppable. So rather than discover them app by app, this says which of the 98 apps carry each one.

  bootstrap_set        the client POSTs {"action":"set"} on load. worker_allowed permits only
                       set_current, so the proxy answers 403 and the app's own hydration swallows
                       it -- google_drive then recorded no write at all from nine clicks, a typed
                       submit and a folder create. On the host, with no proxy in the way, the same
                       call succeeds and reads as a working write path.
  colour_as_class      a state colour field is used where a CSS class is expected, so a hex value
                       renders nothing: google_calendar's MonthView put 297 of 297 events on a
                       white grid in white text.
  hardcoded_record_id  a route or constant names one record id, which no company's world has:
                       monday redirects / to /board/board-1 and shows "Board not found".
  hardcoded_identity   the signed-in person is a module constant, so the seed cannot set who the
                       worker is: instagram browses every company as alex_morgan.
  silent_write_failure the /post handler answers success whether or not the write landed, which is
                       what turns a deleted session directory into a world that reports itself
                       seeded while serving demo records.
  external_host        the page fetches a real domain. In a guest there is no network, so these
                       hang or fall back: calendar loses its fonts, Expensify opens nine dead tabs.
"""

import argparse
import json
import re
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ("*.js", "*.jsx", "*.ts", "*.tsx")

CLASSES = {
    "bootstrap_set": re.compile(r"""action:\s*['"]set['"]"""),
    "colour_as_class": re.compile(r"""\.\w*[Cc]olor\w*\s*\|\|\s*['"](?:bg-|text-|border-)"""),
    "hardcoded_record_id": re.compile(
        r"""(?:to|path)=\{?['"]/[a-z]+/(?:[a-z]+[-_])?(?:1|board-1|default|demo)['"]"""
    ),
    "hardcoded_identity": re.compile(
        r"""const\s+(?:CURRENT_USER(?:_ID)?|LOGGED_IN_USER|ME)\s*=\s*['"][^'"]+['"]"""
    ),
    "external_host": re.compile(r"""https?://(?!localhost|127\.0\.0\.1)([a-z0-9.-]+\.[a-z]{2,})"""),
}
# Served by the harness or rewritten before a worker sees them.
ALLOWED_HOSTS = {"picsum.photos", "via.placeholder.com", "placehold.co", "example.com", "example.test"}


def source_files(app):
    for pattern in CODE:
        yield from (app / "src").rglob(pattern)


def scan_app(app):
    found = defaultdict(list)
    for path in source_files(app):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        where = path.relative_to(app)
        for name, pattern in CLASSES.items():
            for match in pattern.finditer(text):
                if name == "external_host" and match.group(1).lower() in ALLOWED_HOSTS:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                detail = match.group(1) if name == "external_host" else match.group(0)[:60]
                found[name].append(f"{where}:{line} {detail}")
    # The /post handler lives in the vite config, not in src. writeState returns false when the
    # write fails, and a bare call discards that -- the handler then answers {"success": true}
    # regardless, which is what turns a deleted session directory into a world that reports itself
    # seeded while serving the demo records it ships with.
    bare_write = re.compile(r"^\s*writeState\s*\(", re.MULTILINE)
    for config in ("vite.config.js", "vite.config.ts"):
        path = app / config
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for match in bare_write.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found["silent_write_failure"].append(f"{config}:{line} writeState result discarded")
    return {name: sorted(set(hits)) for name, hits in found.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="*")
    parser.add_argument("--hub-root", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    hub = args.hub_root or Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"])
    catalogue = {
        app["id"]
        for app in json.loads((ROOT / "catalogs/apps.json").read_text())["apps"]
        if app.get("schema")
    }
    apps = args.apps or sorted(p.name for p in hub.iterdir() if (p / "src").is_dir())
    report, totals = {}, defaultdict(list)
    for app_id in apps:
        found = scan_app(hub / app_id)
        report[app_id] = {"in_catalogue": app_id in catalogue, "findings": found}
        for name in found:
            totals[name].append(app_id)
    print(f"{len(apps)} hub apps scanned, {len(catalogue & set(apps))} of them in the catalogue\n")
    for name in list(CLASSES) + ["silent_write_failure"]:
        hit = totals.get(name, [])
        listed = [a for a in hit if a in catalogue]
        print(f"{name:<22} {len(listed):>3} catalogue apps  ({len(hit)} of all)")
        if listed:
            print(f"    {', '.join(listed)[:300]}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
