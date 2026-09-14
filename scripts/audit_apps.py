#!/usr/bin/env python3
"""Run browser completeness checks against private copies of built hub apps."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from company_envs.config import load_config
from company_envs.schemas import STANDARD_APPS
from company_envs.world.app_audit import audit_app, write_summary

DEFAULT_APPS = sorted(
    STANDARD_APPS
    | {
        "google_sheets_mock",
        "jira_mock",
        "salesforce_mock",
        "Zendesk_mock",
        "hubspot_mock",
        "quickbooks_mock",
        "clio_mock",
    }
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apps", nargs="*", default=DEFAULT_APPS)
    parser.add_argument("--built", type=Path, default=Path("experiments/hub-cache"))
    parser.add_argument("--schemas", type=Path)
    parser.add_argument("--states", type=Path, default=Path("experiments/hub-smoke/states"))
    parser.add_argument("--probes", type=Path, default=Path("experiments/app-audit/probes.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/app-audit") / datetime.now(UTC).date().isoformat(),
    )
    parser.add_argument("--chromium", type=Path)
    args = parser.parse_args(argv)
    if args.schemas is None:
        args.schemas = Path(load_config(Path(__file__).resolve().parents[1])["design"]["hub_root"])
    # Import only for a live run: unit tests do not need a browser installation.
    from playwright.sync_api import sync_playwright

    plans = json.loads(args.probes.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    reports = []
    with sync_playwright() as playwright:
        launch = {"headless": True, "args": ["--no-sandbox"]}
        if args.chromium:
            launch["executable_path"] = str(args.chromium)
        browser = playwright.chromium.launch(**launch)
        try:
            for app in args.apps:
                if Path(app).name != app or app in {".", ".."}:
                    raise ValueError("App IDs must be plain directory names")
                try:
                    report = audit_app(
                        app,
                        args.built / app,
                        args.schemas / app / "SCHEMA.md",
                        args.states / f"{app}.json",
                        args.output,
                        browser,
                        plans.get(app, {}),
                    )
                except Exception as error:  # noqa: BLE001 - keep failed browser checks in the report
                    report = {
                        "app": app,
                        "error": str(error),
                        "renders": False,
                        "collections_visible": {},
                        "routes_ok": [],
                        "actions_found": [],
                        "write_roundtrip": {"ok": False},
                        "gaps": [{"check": "startup", "gap": str(error)}],
                    }
                    (args.output / f"{app}.json").write_text(json.dumps(report, indent=2) + "\n")
                reports.append(report)
                write_summary(reports, args.output)
                print(
                    f"{app}: renders={report['renders']}, gaps={len(report['gaps'])}, write={report['write_roundtrip']['ok']}",
                    flush=True,
                )
        finally:
            browser.close()
    return int(any(report["gaps"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
