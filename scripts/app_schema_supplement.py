#!/usr/bin/env python3
"""Write the keys each app keeps that its imported schema leaves out.

Seeding is driven by the `## State Schema` table in `catalogs/app_schemas/<app>.md`, which is
imported upstream documentation pinned by hash in `catalogs/manifest.json` -- not ours to correct.
But `validate_state` refuses any key that table omits, so a key the app keeps and renders can
never be seeded: the app falls back to the demo records it ships with, and on load it writes those
into the company's world. Measured on 2026-09-09: 37 of 88 apps, 101 keys, aws_console by 19.

This records those keys instead, from the observations `app_schema_drift.py` captures by serving
each app unseeded and reading the state it keeps -- the same idiom `hub_patches.json` uses for
source amendments. The keys are PERMITTED in a seed, never required, so every world that already
exists still validates. A key becomes required by being documented upstream, once the seeding
skill fills it.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REASON = (
    "The app keeps this key and renders it; the imported schema omits it, so seeding could not "
    "fill it and the app showed the demo records it ships with."
)


def build(reports):
    apps = {}
    for path in sorted(Path(reports).glob("*.json")):
        report = json.loads(path.read_text())
        missing = report.get("undocumented_so_unseedable") or []
        if not missing:
            continue
        shapes = report.get("shapes") or {}
        apps[report["app_id"]] = {
            "observed_from": report.get("source", "unknown"),
            "keys": {
                key: {
                    "kind": shapes.get(key, {}).get("kind"),
                    "keyed_by_id": shapes.get(key, {}).get("keyed_by_id"),
                    "record_fields": (shapes.get(key, {}).get("record_fields") or [])[:40],
                }
                for key in missing
            },
            "reason": REASON,
        }
    return apps


def record_fields(reports):
    """Per app, the field names the app's own records carry, by collection.

    `validate_state` checks top-level keys only, so a seed can rename every field inside a
    collection and still pass. These are the names the app reads, observed from its own state.
    """
    apps = {}
    for path in sorted(Path(reports).glob("*.json")):
        report = json.loads(path.read_text())
        fields = {
            key: shape["record_fields"]
            for key, shape in (report.get("shapes") or {}).items()
            if (shape or {}).get("record_fields")
        }
        if fields:
            apps[report["app_id"]] = fields
    return apps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, required=True, help="an app_schema_drift output dir")
    parser.add_argument("--out", type=Path, default=ROOT / "catalogs/app_schema_supplement.json")
    parser.add_argument("--captured", default="", help="the date the observations were taken")
    parser.add_argument("--fields", type=Path, default=ROOT / "catalogs/app_record_fields.json")
    args = parser.parse_args()
    apps = build(args.reports)
    args.out.write_text(
        json.dumps(
            {
                "captured_at": args.captured or args.reports.name.replace("drift-", ""),
                "how": "scripts/app_schema_drift.py serves each app unseeded and reads the state it keeps.",
                "contract": (
                    "These keys are PERMITTED in a seed but not required. Move a key into the "
                    "imported schema document, and make it required, only once the seeding skill "
                    "fills it."
                ),
                "apps": apps,
            },
            indent=2,
        )
        + "\n"
    )
    fields = record_fields(args.reports)
    args.fields.write_text(
        json.dumps(
            {
                "captured_at": args.captured or args.reports.name.replace("drift-", ""),
                "how": "scripts/app_schema_drift.py serves each app unseeded and reads its own records.",
                "contract": (
                    "The field names each app's own records carry, by collection. A seeded record "
                    "that renames them passes validate_state and renders nothing."
                ),
                "apps": fields,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{args.fields}: {len(fields)} apps, {sum(len(c) for c in fields.values())} collections")
    keys = sum(len(app["keys"]) for app in apps.values())
    print(f"{args.out}: {len(apps)} apps, {keys} keys the imported schemas leave unseedable")
    for app_id, app in sorted(apps.items(), key=lambda kv: -len(kv[1]["keys"]))[:10]:
        print(f"  {app_id:<26} {len(app['keys']):>3}  {', '.join(sorted(app['keys']))[:90]}")


if __name__ == "__main__":
    main()
