#!/usr/bin/env python3
"""Click through seeded hub apps and report what works, one app after another.

Two modes answer two different questions. ``fidelity`` seeds a real company's world state, so
the report says whether that company's records reach the screen and whether a worker's action
reaches the state a grader reads. ``volume`` grows a small fixture state to the size a real
company reaches, so the report says whether the app survives that size at all.
"""

import argparse
import json
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from company_envs.storage import read, write
from company_envs.world.interact_check import MAX_CLICKS, amplify, interact_app


def world_state(app_id, companies):
    """The largest real seeded state for this app: the most demanding world we actually built."""
    states = sorted(
        (path for path in companies.glob(f"*/world/{app_id}.state.json")),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    return states[0] if states else None


def pick_state(app_id, mode, fixtures, companies, target_bytes):
    """Which state to seed, where it came from, and what growing it managed.

    The third value is only ever set in volume mode, and it is what keeps a run honest: an app
    whose state could not be grown to the target has not been volume tested, and must not file
    the report a volume test files.
    """
    real = world_state(app_id, companies)
    if mode == "fidelity":
        return (read(real), f"world:{real.parent.parent.name}", None) if real else (None, "none", None)
    fixture = fixtures / f"{app_id}.json"
    source = fixture if fixture.is_file() else real
    if source is None:
        return None, "none", None
    grown, amplified = amplify(read(source), target_bytes)
    origin = "fixture" if source is fixture else f"world:{source.parent.parent.name}"
    suffix = "amplified" if amplified["reached_target"] else "not-amplified"
    return grown, f"{origin}+{suffix}", amplified


def record_volume_reach(report, amplified):
    """Say so in the report when the state never reached the size the run claims to have tested.

    canvas_mock filed a ``mode: volume`` report over a 1,764-byte state against a 3 MB target --
    the class of defect where an empty run writes the receipt a real verdict writes. The run's
    other findings are still worth keeping, so they are kept, under a mode nobody can mistake
    for a volume verdict and a failed check that names what was not done.
    """
    report["amplification"] = amplified
    report["volume_tested"] = bool(amplified["reached_target"])
    if amplified["reached_target"]:
        return report
    report["mode"] = "volume-not-reached"
    report["note"] = (
        f"state grew {amplified['start_bytes']} -> {amplified['bytes']} bytes against a target of"
        f" {amplified['target_bytes']}: this run is not a volume verdict"
    )
    unreached = "state_reached_volume_target"
    report["checks"] = {**(report.get("checks") or {}), unreached: False}
    report["failed_checks"] = sorted({*(report.get("failed_checks") or []), unreached})
    report["passed"] = False
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="+")
    parser.add_argument("--mode", choices=("fidelity", "volume"), default="fidelity")
    parser.add_argument("--target-mb", type=float, default=3.0, help="volume mode: size to grow to")
    parser.add_argument("--work", type=Path, default=ROOT / "experiments/hub-cache")
    parser.add_argument("--hub-root", type=Path)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "experiments/hub-smoke/states")
    parser.add_argument("--companies", type=Path, default=ROOT / "companies")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-clicks", type=int, default=MAX_CLICKS)
    parser.add_argument(
        "--probes",
        type=Path,
        default=ROOT / "experiments/app-audit/probes.json",
        help="hand-authored write recipes; an app with one is measured by it, not by the search",
    )
    parser.add_argument(
        "--generic-write",
        action="store_true",
        help="ignore the recipes and let the generic probe look for a write, as this run used to",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete node_modules after each app; the build stays cached and vite is reinstalled on demand",
    )
    args = parser.parse_args(argv)

    hub_root = args.hub_root or Path(tomllib.loads((ROOT / "config.toml").read_text())["design"]["hub_root"])
    # The generic probe types into whatever box it can find; a recipe names the control and the
    # collection the write has to reach. Measured over 88 apps the difference is 20 apps against
    # 75, so a report that consults only the generic probe understates the catalogue by 55.
    plans = {} if args.generic_write or not args.probes.is_file() else json.loads(args.probes.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    target_bytes = int(args.target_mb * 1024 * 1024)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch(args=["--no-sandbox"])
        try:
            for app_id in args.apps:
                if Path(app_id).name != app_id or app_id in {".", ".."}:
                    raise ValueError("App ids must be plain directory names")
                started = datetime.now(UTC)
                state, origin, amplified = pick_state(
                    app_id, args.mode, args.fixtures, args.companies, target_bytes
                )
                try:
                    report = interact_app(
                        app_id,
                        hub_root / app_id,
                        args.work,
                        state,
                        browser,
                        sid=f"interact-{args.mode}",
                        max_clicks=args.max_clicks,
                        shot=args.out / "shots" / f"{app_id}.png",
                        log_dir=args.out / "logs",
                        plan=(plans.get(app_id) or {}).get("write"),
                    )
                except Exception as exc:  # noqa: BLE001 -- a harness failure is a result about the app
                    report = {
                        "app_id": app_id,
                        "passed": False,
                        "failed_checks": ["harness"],
                        "harness_error": f"{type(exc).__name__}: {exc}"[:600],
                    }
                report["mode"] = args.mode
                report["state_origin"] = origin
                report["write_plan"] = bool((plans.get(app_id) or {}).get("write"))
                if amplified:
                    record_volume_reach(report, amplified)
                report["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 1)
                if args.prune:
                    shutil.rmtree(args.work / app_id / "node_modules", ignore_errors=True)
                write(args.out / f"{app_id}.json", report)
                print(
                    f"{'PASS' if report.get('passed') else 'FAIL'} {app_id:<28} {origin:<28}"
                    f" {report['seconds']:>6}s  {','.join(report.get('failed_checks', []))[:90]}",
                    flush=True,
                )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
